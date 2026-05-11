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

  var logOffset = 0;
  var lastStatus = pageEl.getAttribute('data-initial-status') || 'running';
  var pollTimer = null;
  var stopped = false;

  function setStatusBadge(status) {
    statusContainer.innerHTML = '';
    if (status === 'running') {
      statusContainer.innerHTML =
        '<span class="inline-block w-3 h-3 border-2 border-zinc-300 border-t-zinc-600 rounded-full animate-spin"></span>' +
        '<span class="text-zinc-700">Running...</span>';
    } else if (status === 'failed') {
      statusContainer.innerHTML =
        '<span class="inline-flex items-center px-2 py-0.5 rounded text-sm font-medium bg-red-100 text-red-800">Failed</span>';
    } else if (status === 'done') {
      statusContainer.innerHTML =
        '<span class="inline-flex items-center px-2 py-0.5 rounded text-sm font-medium bg-emerald-100 text-emerald-800">Done. Redirecting...</span>';
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
        return data.status;
      })
      .catch(function () { return null; });
  }

  function tick() {
    if (stopped) return;
    Promise.all([fetchLog(), fetchStatus()]).then(function (results) {
      var status = results[1];
      if (status && status !== lastStatus) {
        lastStatus = status;
        setStatusBadge(status);
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
          actionsEl.classList.add('hidden');
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
