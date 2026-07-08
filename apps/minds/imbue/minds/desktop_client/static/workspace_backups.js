// Backup section of the workspace settings page: shows the combined
// snapshot status + backup-service verification breakdown from
// /api/v1/workspaces/backup-health, and drives the three actions:
//   - "Update backup service" (one idempotent converge; tracked operation
//     polled at /api/v1/workspaces/operations/backup/<id>, with a
//     "Stop all chats and retry" follow-up when running chats block it and
//     a Cancel that works while the update is still waiting),
//   - "Configure backups..." (enable on a configure-later workspace, or
//     change the destination; same tracked-operation polling),
//   - the per-workspace "verify backups" toggle.
(function () {
  var section = document.getElementById('backup-section');
  if (!section) return;
  var agentId = document.getElementById('workspace-settings').dataset.agentId;
  var hasSavedPassword = section.dataset.hasSavedPassword === 'true';

  var statusLine = document.getElementById('backup-status-line');
  var versionsEl = document.getElementById('backup-versions');
  var problemsEl = document.getElementById('backup-problems');
  var updateBtn = document.getElementById('backup-update-btn');
  var stopChatsBtn = document.getElementById('backup-stop-chats-btn');
  var cancelBtn = document.getElementById('backup-cancel-btn');
  var spinner = document.getElementById('backup-op-spinner');
  var progressEl = document.getElementById('backup-op-progress');
  var errorEl = document.getElementById('backup-error');
  var verificationToggle = document.getElementById('backup-verification-toggle');

  var configureToggleBtn = document.getElementById('backup-configure-toggle-btn');
  var configureForm = document.getElementById('backup-configure-form');
  var providerSelect = document.getElementById('backup-provider-select');
  var encryptionSelect = document.getElementById('backup-encryption-select');
  var masterPasswordRow = document.getElementById('backup-master-password-row');
  var masterPasswordInput = document.getElementById('backup-master-password-input');
  var savePasswordInput = document.getElementById('backup-save-password-input');
  var apiKeyRow = document.getElementById('backup-api-key-row');
  var apiKeyEnvInput = document.getElementById('backup-api-key-env-input');
  var configureSubmitBtn = document.getElementById('backup-configure-submit-btn');

  var PROBLEM_LABELS = {
    NOT_CONFIGURED: 'Backups are not configured for this workspace.',
    CODE_OUTDATED: 'The backup service code is outdated.',
    ENV_MISSING: 'The backup credentials file is missing on the workspace.',
    ENV_MISMATCH: "The workspace's backup credentials don't match the expected configuration.",
    SERVICE_NOT_RUNNING: 'The backup service is not running.',
    UNVERIFIABLE: 'The backup service could not be verified.',
  };

  function showError(message) {
    errorEl.textContent = message;
    errorEl.classList.remove('hidden');
  }
  function clearError() {
    errorEl.classList.add('hidden');
  }

  function snapshotText(entry) {
    if (entry.snapshot_state === 'BACKING_UP') return 'Backing up now...';
    if (entry.snapshot_state === 'BACKED_UP' && entry.last_success_at) {
      return 'Last backup: ' + new Date(entry.last_success_at).toLocaleString();
    }
    if (entry.snapshot_state === 'NEVER') return 'No successful backup yet.';
    if (entry.snapshot_state === 'NOT_CONFIGURED') return 'Backups are not configured.';
    return 'Backup status unknown.';
  }

  function renderEntry(entry) {
    statusLine.textContent = snapshotText(entry);
    verificationToggle.checked = !!entry.is_verification_enabled;

    problemsEl.textContent = '';
    problemsEl.classList.add('hidden');
    versionsEl.classList.add('hidden');
    updateBtn.classList.add('hidden');

    if (entry.check_state === 'DISABLED') {
      statusLine.textContent += ' Backup verification is disabled for this workspace.';
      return;
    }
    if (entry.check_state === 'OFFLINE') {
      statusLine.textContent += ' The workspace is offline; its backup service will be verified when it is back.';
      return;
    }
    if (entry.installed_version || entry.desired_version) {
      versionsEl.textContent =
        'Installed backup service: ' + (entry.installed_version || 'unknown') +
        ' / expected: ' + (entry.desired_version || 'unknown');
      versionsEl.classList.remove('hidden');
    }
    if (entry.check_state === 'PROBLEMS') {
      (entry.problems || []).forEach(function (problem) {
        var li = document.createElement('li');
        li.textContent = PROBLEM_LABELS[problem] || problem;
        problemsEl.appendChild(li);
      });
      if (entry.detail) {
        var detailLi = document.createElement('li');
        detailLi.textContent = entry.detail;
        problemsEl.appendChild(detailLi);
      }
      problemsEl.classList.remove('hidden');
      updateBtn.classList.remove('hidden');
    } else if (entry.check_state === 'OK') {
      statusLine.textContent += ' The backup service is up to date.';
    }
  }

  function refreshHealth() {
    fetch('/api/v1/workspaces/backup-health')
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (data) {
        if (!data) return;
        var entry = (data.workspaces || []).find(function (w) { return w.agent_id === agentId; });
        if (entry) renderEntry(entry);
        else statusLine.textContent = 'Backup status unavailable for this workspace.';
        if (window.mindsBackupHealth) window.mindsBackupHealth.ingest(data);
      })
      .catch(function () {
        statusLine.textContent = 'Could not load backup status.';
      });
  }

  // -- Tracked operation driving (update + configure share the poller) ------

  // Cancel only affects a still-waiting backup *update* (the cancel route
  // 404s for configure operations, which have no waiting phase), so the
  // Cancel button is shown only for cancellable operations.
  function setOperationRunning(isRunning, isCancellable) {
    spinner.classList.toggle('hidden', !isRunning);
    updateBtn.disabled = isRunning;
    configureSubmitBtn.disabled = isRunning;
    stopChatsBtn.disabled = isRunning;
    cancelBtn.classList.toggle('hidden', !(isRunning && isCancellable));
    if (!isRunning) progressEl.classList.add('hidden');
  }

  function streamOperationLogs() {
    var source = new EventSource('/api/v1/workspaces/operations/backup/' + encodeURIComponent(agentId) + '/logs');
    source.onmessage = function (event) {
      try {
        var frame = JSON.parse(event.data);
        if (frame.log) {
          progressEl.textContent = frame.log;
          progressEl.classList.remove('hidden');
        }
        if (frame.done) source.close();
      } catch (e) { /* keepalive frames etc. */ }
    };
    source.onerror = function () { source.close(); };
    return source;
  }

  function pollOperation() {
    fetch('/api/v1/workspaces/operations/backup/' + encodeURIComponent(agentId))
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (op) {
        if (!op) { setOperationRunning(false); return; }
        if (op.status === 'RUNNING') {
          setTimeout(pollOperation, 2000);
          return;
        }
        setOperationRunning(false);
        stopChatsBtn.classList.add('hidden');
        if (op.is_done) {
          refreshHealth();
          return;
        }
        if (op.blocked_chats && op.blocked_chats.length > 0) {
          showError(
            'Chats are running in this workspace (' + op.blocked_chats.join(', ') +
            '). Stop them before updating the backup service; they resume on your next message.'
          );
          stopChatsBtn.classList.remove('hidden');
          return;
        }
        showError(op.error || 'The backup operation failed.');
      })
      .catch(function () { setOperationRunning(false); });
  }

  function startOperation(url, body, isCancellable) {
    clearError();
    setOperationRunning(true, isCancellable);
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    })
      .then(function (resp) {
        if (resp.status === 202) {
          streamOperationLogs();
          pollOperation();
          return null;
        }
        return resp.json().then(function (data) {
          setOperationRunning(false);
          showError((data && (data.error || data.message)) || ('Request failed (HTTP ' + resp.status + ')'));
        });
      })
      .catch(function () {
        setOperationRunning(false);
        showError('Request failed (network error).');
      });
  }

  updateBtn.addEventListener('click', function () {
    startOperation('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backup-service/update', { stop_chats: false }, true);
  });
  stopChatsBtn.addEventListener('click', function () {
    stopChatsBtn.classList.add('hidden');
    startOperation('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backup-service/update', { stop_chats: true }, true);
  });
  cancelBtn.addEventListener('click', function () {
    fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backup-service/update/cancel', { method: 'POST' })
      .catch(function () {});
  });

  // -- Configure form -------------------------------------------------------

  function syncConfigureFormVisibility() {
    var isApiKey = providerSelect.value === 'API_KEY';
    apiKeyRow.classList.toggle('hidden', !isApiKey);
    if (masterPasswordRow) {
      var needsPassword = encryptionSelect.value === 'MASTER_PASSWORD' && !hasSavedPassword;
      masterPasswordRow.classList.toggle('hidden', !needsPassword);
    }
  }
  configureToggleBtn.addEventListener('click', function () {
    configureForm.classList.toggle('hidden');
    syncConfigureFormVisibility();
  });
  providerSelect.addEventListener('change', syncConfigureFormVisibility);
  encryptionSelect.addEventListener('change', syncConfigureFormVisibility);

  configureSubmitBtn.addEventListener('click', function () {
    var body = {
      backup_provider: providerSelect.value,
      backup_encryption_method: encryptionSelect.value,
      api_key_env: apiKeyEnvInput ? apiKeyEnvInput.value : '',
      master_password: masterPasswordInput ? masterPasswordInput.value : '',
      save_password: savePasswordInput ? savePasswordInput.checked : false,
    };
    startOperation('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backup-service/configure', body, false);
  });

  // -- Verification toggle --------------------------------------------------

  verificationToggle.addEventListener('change', function () {
    fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backup-service/verification', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: verificationToggle.checked }),
    })
      .then(function (resp) {
        if (!resp.ok) showError('Could not update the verification setting (HTTP ' + resp.status + ').');
        else refreshHealth();
      })
      .catch(function () { showError('Could not update the verification setting (network error).'); });
  });

  refreshHealth();
})();
