// Backup section of the workspace settings page: shows the combined
// snapshot status + backup-service verification breakdown from this
// workspace's /api/v1/workspaces/<id>/backups, and drives the actions:
//   - the verification Enable/Disable button (top of the section: the whole
//     breakdown below it only exists while verification is on),
//   - "Update backup service" (one idempotent converge; tracked operation
//     polled at /api/v1/workspaces/operations/backup/<id>, with a
//     "Stop all chats and retry" follow-up when running chats block it and
//     a Cancel that works while the update is still waiting),
//   - "Configure backups..." (enable, change destination, or disable via the
//     "None" provider; same tracked-operation polling).
//
// Conditional buttons are shown/hidden via their wrapper spans (a `hidden`
// class directly on a Button loses to its inline-flex display class).
(function () {
  var section = document.getElementById('backup-section');
  if (!section) return;
  var agentId = document.getElementById('workspace-settings').dataset.agentId;

  var statusLine = document.getElementById('backup-status-line');
  var versionsEl = document.getElementById('backup-versions');
  var problemsEl = document.getElementById('backup-problems');
  var updateBtn = document.getElementById('backup-update-btn');
  var updateBtnWrap = document.getElementById('backup-update-btn-wrap');
  var stopChatsBtn = document.getElementById('backup-stop-chats-btn');
  var stopChatsBtnWrap = document.getElementById('backup-stop-chats-btn-wrap');
  var cancelBtn = document.getElementById('backup-cancel-btn');
  var cancelBtnWrap = document.getElementById('backup-cancel-btn-wrap');
  var spinner = document.getElementById('backup-op-spinner');
  var progressEl = document.getElementById('backup-op-progress');
  var errorEl = document.getElementById('backup-error');
  var verificationBtn = document.getElementById('backup-verification-btn');
  var verificationBtnWrap = document.getElementById('backup-verification-btn-wrap');
  var verificationSpinner = document.getElementById('backup-verification-spinner');

  var configureToggleBtn = document.getElementById('backup-configure-toggle-btn');
  var configureForm = document.getElementById('backup-configure-form');
  var providerSelect = document.getElementById('backup-provider-select');
  var apiKeyRow = document.getElementById('backup-api-key-row');
  var apiKeyEnvInput = document.getElementById('backup-api-key-env-input');
  var configureSubmitBtn = document.getElementById('backup-configure-submit-btn');

  // The latest known verification state, driving the Enable/Disable label.
  var isVerificationEnabled = true;

  var PROBLEM_LABELS = {
    NOT_CONFIGURED: 'Backups are not configured for this workspace.',
    CODE_OUTDATED: 'The backup service code is outdated.',
    ENV_MISSING: 'The backup credentials file is missing on the workspace.',
    ENV_MISMATCH: "The workspace's backup credentials don't match the expected configuration.",
    SERVICE_NOT_RUNNING: 'The backup service is not running.',
    UNVERIFIABLE: 'The backup service could not be verified.',
  };

  function setShown(el, isShown) {
    if (el) el.classList.toggle('hidden', !isShown);
  }

  function showError(message) {
    errorEl.textContent = message;
    errorEl.classList.remove('hidden');
  }
  function clearError() {
    errorEl.classList.add('hidden');
  }

  function latestSnapshotTime(snapshots) {
    var latest = null;
    (snapshots || []).forEach(function (snapshot) {
      if (!latest || Date.parse(snapshot.time) > Date.parse(latest)) latest = snapshot.time;
    });
    return latest;
  }

  function snapshotText(entry) {
    if (entry.is_backing_up) return 'Backing up now...';
    var latest = latestSnapshotTime(entry.snapshots);
    if (latest) return 'Last backup: ' + new Date(latest).toLocaleString();
    if (!entry.is_configured) return 'Backups are not configured.';
    if (entry.snapshots_error) return 'Backup status unknown.';
    return 'No successful backup yet.';
  }

  function renderEntry(entry) {
    statusLine.textContent = snapshotText(entry);
    isVerificationEnabled = !!entry.is_verification_enabled;
    verificationBtn.textContent = isVerificationEnabled ? 'Disable' : 'Enable';
    setShown(verificationBtnWrap, true);

    problemsEl.textContent = '';
    problemsEl.classList.add('hidden');
    versionsEl.classList.add('hidden');
    setShown(updateBtnWrap, false);

    if (entry.check_state === 'DISABLED') {
      statusLine.textContent += ' Backup service verification is disabled for this workspace.';
      // The update is an idempotent converge and does not depend on
      // verification, so it stays available.
      setShown(updateBtnWrap, true);
      return;
    }
    if (entry.check_state === 'OFFLINE') {
      statusLine.textContent += ' The workspace is offline; its backup service will be verified when it is back.';
      return;
    }
    var versionParts = [];
    if (entry.installed_version) versionParts.push('Installed backup service: ' + entry.installed_version);
    if (entry.minimum_version) versionParts.push('minimum required: ' + entry.minimum_version);
    if (entry.update_target_version && entry.update_target_version !== entry.minimum_version) {
      versionParts.push('update installs: ' + entry.update_target_version);
    }
    if (versionParts.length > 0) {
      versionsEl.textContent = versionParts.join(' / ');
      versionsEl.classList.remove('hidden');
    }
    // The update is an idempotent converge, so the button is always offered
    // for a reachable workspace -- even at the target version it usefully
    // resets a wedged backup service.
    setShown(updateBtnWrap, true);
    if (entry.check_state === 'PROBLEMS') {
      (entry.problems || []).forEach(function (problem) {
        var li = document.createElement('li');
        li.textContent = PROBLEM_LABELS[problem] || problem;
        problemsEl.appendChild(li);
      });
      if (entry.check_detail) {
        var detailLi = document.createElement('li');
        detailLi.textContent = entry.check_detail;
        problemsEl.appendChild(detailLi);
      }
      problemsEl.classList.remove('hidden');
    } else if (entry.check_state === 'OK') {
      statusLine.textContent += ' The backup service is up to date.';
    }
  }

  function refreshHealth() {
    fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backups')
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (entry) {
        if (!entry) {
          statusLine.textContent = 'Backup status unavailable for this workspace.';
          return;
        }
        renderEntry(entry);
        if (window.mindsBackupHealth) window.mindsBackupHealth.ingestEntry(entry);
      })
      .catch(function () {
        statusLine.textContent = 'Could not load backup status.';
      });
  }

  // -- Verification Enable/Disable ------------------------------------------

  verificationBtn.addEventListener('click', function () {
    clearError();
    var targetEnabled = !isVerificationEnabled;
    setShown(verificationBtnWrap, false);
    verificationSpinner.textContent = targetEnabled ? 'Enabling...' : 'Disabling...';
    verificationSpinner.classList.remove('hidden');
    fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backup-service/verification', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: targetEnabled }),
    })
      .then(function (resp) {
        if (!resp.ok) {
          verificationSpinner.classList.add('hidden');
          setShown(verificationBtnWrap, true);
          showError('Could not update the verification setting (HTTP ' + resp.status + ').');
          return null;
        }
        // Re-fetching also re-runs the (possibly slow) service check; the
        // spinner keeps showing what we're doing until the fresh state lands.
        return fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backups')
          .then(function (entryResp) { return entryResp.ok ? entryResp.json() : null; })
          .then(function (entry) {
            verificationSpinner.classList.add('hidden');
            if (entry) {
              renderEntry(entry);
              if (window.mindsBackupHealth) window.mindsBackupHealth.ingestEntry(entry);
            } else {
              setShown(verificationBtnWrap, true);
            }
          });
      })
      .catch(function () {
        verificationSpinner.classList.add('hidden');
        setShown(verificationBtnWrap, true);
        showError('Could not update the verification setting (network error).');
      });
  });

  // -- Tracked operation driving (update + configure/disable share the poller)

  // Cancel only affects a still-waiting backup *update* (the cancel route
  // 404s for configure operations, which have no waiting phase), so the
  // Cancel button is shown only for cancellable operations.
  function setOperationRunning(isRunning, isCancellable) {
    spinner.classList.toggle('hidden', !isRunning);
    updateBtn.disabled = isRunning;
    configureSubmitBtn.disabled = isRunning;
    stopChatsBtn.disabled = isRunning;
    setShown(cancelBtnWrap, isRunning && isCancellable);
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
        setShown(stopChatsBtnWrap, false);
        if (op.is_done) {
          refreshHealth();
          return;
        }
        if (op.blocked_chats && op.blocked_chats.length > 0) {
          showError(
            'Chats are running in this workspace (' + op.blocked_chats.join(', ') +
            '). Stop them before updating the backup service; they resume on your next message.'
          );
          setShown(stopChatsBtnWrap, true);
          return;
        }
        showError(op.error || 'The backup operation failed.');
      })
      // A transient fetch failure must not end the Working state while the
      // backend operation is still running -- keep polling.
      .catch(function () { setTimeout(pollOperation, 2000); });
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
    setShown(stopChatsBtnWrap, false);
    startOperation('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backup-service/update', { stop_chats: true }, true);
  });
  cancelBtn.addEventListener('click', function () {
    fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backup-service/update/cancel', { method: 'POST' })
      .catch(function () {});
  });

  // -- Configure form (enable / change destination / disable) ---------------

  function syncConfigureFormVisibility() {
    var provider = providerSelect.value;
    apiKeyRow.classList.toggle('hidden', provider !== 'API_KEY');
  }
  configureToggleBtn.addEventListener('click', function () {
    configureForm.classList.toggle('hidden');
    syncConfigureFormVisibility();
  });
  providerSelect.addEventListener('change', syncConfigureFormVisibility);

  // No password is involved: repositories are keyed by each workspace's own
  // random password, and the master password's only role is wrapping the
  // account's sync key (see the app-level Settings page).
  configureSubmitBtn.addEventListener('click', function () {
    if (providerSelect.value === 'NONE') {
      startOperation('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backup-service/disable', {}, false);
      return;
    }
    var body = {
      backup_provider: providerSelect.value,
      api_key_env: apiKeyEnvInput ? apiKeyEnvInput.value : '',
    };
    startOperation('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backup-service/configure', body, false);
  });

  refreshHealth();
})();
