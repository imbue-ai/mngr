// Record-backed create attempt detail page (interrupted / failed rows): wires the
// Discard / Dismiss actions against the create attempts API and, for a discard,
// streams the detached destroy's output + polls its status -- the same
// poll+SSE shape as the workspace destroy page (destroying.js). Reads the
// create attempt id from #create-attempt-record-page data-create-attempt-id so the template
// stays JS-free.
(function () {
  var pageEl = document.getElementById('create-attempt-record-page');
  if (!pageEl) return;
  var createAttemptId = pageEl.getAttribute('data-create-attempt-id');
  var recordView = document.getElementById('record-view');
  var discardView = document.getElementById('discard-view');
  var discardStatusEl = document.getElementById('discard-status');
  var discardLogEl = document.getElementById('discard-log');
  var discardActionsEl = document.getElementById('discard-actions');
  var errorEl = document.getElementById('create-attempt-record-error');

  var statusPoll = null;
  var source = null;
  var stopped = false;

  function showError(message) {
    if (!errorEl) return;
    errorEl.textContent = message;
    errorEl.classList.remove('hidden');
  }

  function appendLog(content) {
    if (!content || !discardLogEl) return;
    discardLogEl.appendChild(document.createTextNode(content));
    discardLogEl.scrollTop = discardLogEl.scrollHeight;
  }

  function stopPolling() {
    if (statusPoll) { clearInterval(statusPoll); statusPoll = null; }
  }

  function closeSource() {
    if (source) { source.close(); source = null; }
  }

  function setDiscardStatus(status) {
    if (!discardStatusEl) return;
    if (status === 'running') {
      discardStatusEl.innerHTML =
        '<span class="spinner inline-block w-3 h-3 align-middle"></span>' +
        '<span class="text-primary">Cleaning up...</span>';
    } else if (status === 'failed') {
      discardStatusEl.innerHTML =
        '<span class="inline-flex items-center px-2 py-0.5 rounded-md type-label bg-important/15 text-important">Cleanup failed</span>';
    } else if (status === 'done') {
      discardStatusEl.innerHTML =
        '<span class="inline-flex items-center px-2 py-0.5 rounded-md type-label bg-success/15 text-success">Done. Redirecting...</span>';
    }
  }

  // Apply an authoritative discard status. The status endpoint 404s once a
  // finished discard has been finalized (record deleted), which also counts
  // as done.
  function applyDiscardStatus(status) {
    if (!status || stopped) return;
    setDiscardStatus(status);
    if (status === 'done') {
      stopped = true;
      stopPolling();
      closeSource();
      window.setTimeout(function () { window.location.href = '/'; }, 800);
    } else if (status === 'failed') {
      stopped = true;
      stopPolling();
      closeSource();
      if (discardActionsEl) discardActionsEl.classList.remove('hidden');
    }
  }

  function pollDiscardStatus() {
    fetch('/api/v1/workspaces/operations/create-attempt-discard/' + encodeURIComponent(createAttemptId))
      .then(function (resp) {
        if (resp.status === 404) {
          // Finalized between polls (another window observed DONE first).
          applyDiscardStatus('done');
          return null;
        }
        return resp.ok ? resp.json() : null;
      })
      .then(function (data) {
        if (data && data.status) applyDiscardStatus(String(data.status).toLowerCase());
      })
      .catch(function () {});
  }

  function openDiscardLogSource() {
    closeSource();
    source = new EventSource(
      '/api/v1/workspaces/operations/create-attempt-discard/' + encodeURIComponent(createAttemptId) + '/logs'
    );
    source.onmessage = function (event) {
      var data;
      try { data = JSON.parse(event.data); } catch (e) { return; }
      if (data.log) appendLog(data.log);
      if (data.done) {
        closeSource();
        if (data.status) applyDiscardStatus(String(data.status).toLowerCase());
      }
    };
    source.onerror = function () { closeSource(); };
  }

  function startDiscard(button) {
    if (button) button.disabled = true;
    fetch('/api/v1/workspaces/create-attempts/' + encodeURIComponent(createAttemptId) + '/discard', { method: 'POST' })
      .then(function (resp) {
        if (!resp.ok) {
          if (button) button.disabled = false;
          return resp.json().then(
            function (data) { showError((data && data.error) || 'Could not start the cleanup.'); },
            function () { showError('Could not start the cleanup.'); }
          );
        }
        // Swap to the cleanup view and start streaming the destroy output.
        stopped = false;
        if (recordView) recordView.classList.add('hidden');
        if (discardView) discardView.classList.remove('hidden');
        if (discardActionsEl) discardActionsEl.classList.add('hidden');
        if (discardLogEl) discardLogEl.textContent = '';
        setDiscardStatus('running');
        openDiscardLogSource();
        pollDiscardStatus();
        stopPolling();
        statusPoll = setInterval(pollDiscardStatus, 1000);
        return null;
      })
      .catch(function () {
        if (button) button.disabled = false;
        showError('Could not reach the server. Please try again.');
      });
  }

  var discardBtn = document.getElementById('create-attempt-discard-btn');
  if (discardBtn) {
    discardBtn.addEventListener('click', function () { startDiscard(discardBtn); });
  }
  var discardRetryBtn = document.getElementById('discard-retry-btn');
  if (discardRetryBtn) {
    discardRetryBtn.addEventListener('click', function () { startDiscard(discardRetryBtn); });
  }

  var dismissBtn = document.getElementById('create-attempt-dismiss-btn');
  if (dismissBtn) {
    dismissBtn.addEventListener('click', function () {
      dismissBtn.disabled = true;
      fetch('/api/v1/workspaces/create-attempts/' + encodeURIComponent(createAttemptId), { method: 'DELETE' })
        .finally(function () { window.location.href = '/'; });
    });
  }
})();
