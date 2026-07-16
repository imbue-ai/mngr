// Backup section of the workspace settings page: shows the combined
// snapshot status + backup-service verification breakdown from this
// workspace's /api/v1/workspaces/<id>/backups, and drives the actions:
//   - the verification Enable/Disable button (the whole problem/version
//     breakdown only exists while it is on),
//   - "Update backup software" (one idempotent converge),
//   - "Change storage location" (enable, change destination, or disable via
//     the "None" provider),
//   - per-row "Restore" in the Recent backups table (confirm dialog, then an
//     in-place restore).
//
// All of these run as tracked operations reported through the shared
// operation strip; backup_operation_ui.js owns that machinery (polling,
// streamed progress, Cancel, "Stop chats and try again", success/error
// messages) and reattaches to an operation already RUNNING on load --
// including one started from the backup-history page.
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
  var verificationBtn = document.getElementById('backup-verification-btn');
  var verificationBtnWrap = document.getElementById('backup-verification-btn-wrap');
  var verificationSpinner = document.getElementById('backup-verification-spinner');

  var configureToggleBtn = document.getElementById('backup-configure-toggle-btn');
  var configureForm = document.getElementById('backup-configure-form');
  var providerSelect = document.getElementById('backup-provider-select');
  var apiKeyRow = document.getElementById('backup-api-key-row');
  var apiKeyEnvInput = document.getElementById('backup-api-key-env-input');
  var configureSubmitBtn = document.getElementById('backup-configure-submit-btn');

  var historyCard = document.getElementById('backup-history-card');
  var historyEl = document.getElementById('backup-history');
  var historyEmptyEl = document.getElementById('backup-history-empty');
  var viewAllLink = document.getElementById('backup-view-all');
  var viewAllLabel = document.getElementById('backup-view-all-label');

  // The latest known verification state, driving the Enable/Disable label.
  var isVerificationEnabled = true;

  var RECENT_LIMIT = 5;

  var PROBLEM_LABELS = {
    NOT_CONFIGURED: 'Backups are turned off for this workspace. Use "Change storage location" to turn them on.',
    CODE_OUTDATED: 'The backup software in this workspace is out of date. Click "Update backup software" to fix this.',
    ENV_MISSING: 'This workspace has lost its backup storage settings. Click "Update backup software" to restore them.',
    ENV_MISMATCH: 'This workspace is set up to back up somewhere different than expected. Click "Update backup software" to fix this.',
    SERVICE_NOT_RUNNING: 'The backup software in this workspace is not running. Click "Update backup software" to restart it.',
    UNVERIFIABLE: "minds couldn't check on this workspace's backups. Click \"Update backup software\" to reset them.",
  };

  function setShown(el, isShown) {
    if (el) el.classList.toggle('hidden', !isShown);
  }

  // The shared operation-strip driver; page-specific hooks disable this
  // page's action buttons while an operation runs and refresh the health
  // panel once one succeeds.
  var opUi = window.mindsBackupOperationUi.setup({
    agentId: agentId,
    onRunningChange: function (isRunning) {
      updateBtn.disabled = isRunning;
      configureSubmitBtn.disabled = isRunning;
      // The verification toggle only writes a local setting, but its re-check
      // execs into the workspace -- mid-operation (services stopped or
      // restarting) that would flash a confusing transient result.
      verificationBtn.disabled = isRunning;
      disableLiveRestoreButtons(isRunning);
    },
    onSuccess: function () { refreshHealth(); },
  });

  // Only the live restore buttons (text-accent); the offline-disabled ones
  // must stay disabled when the operation ends.
  function disableLiveRestoreButtons(isDisabled) {
    section.querySelectorAll('.backup-restore-btn.text-accent').forEach(function (btn) {
      btn.disabled = isDisabled;
    });
  }

  function snapshotText(entry) {
    if (entry.is_backing_up) return 'Backing up now...';
    var latest = entry.snapshots && entry.snapshots.length > 0 ? entry.snapshots[0].time : null;
    if (latest) return 'Last backup: ' + new Date(latest).toLocaleString();
    if (!entry.is_configured) return 'Backups are not configured.';
    if (entry.snapshots_error) return 'Backup status unknown.';
    return 'No successful backup yet.';
  }

  // -- Backup history list --------------------------------------------------

  // Render the "Recent backups" table from the /backups entry. Newest
  // RECENT_LIMIT rows; "View all" links to the full-history page when needed.
  function renderHistory(entry) {
    historyEl.textContent = '';
    setShown(historyCard, false);
    historyEmptyEl.classList.add('hidden');

    if (!entry.is_configured) {
      historyEmptyEl.textContent = 'Backups are turned off for this workspace. Use "Change storage location" to turn them on.';
      historyEmptyEl.classList.remove('hidden');
      return;
    }
    if (entry.snapshots_error) {
      historyEmptyEl.textContent = "Couldn't load your backup history right now.";
      historyEmptyEl.classList.remove('hidden');
      return;
    }
    var snapshots = entry.snapshots || [];
    if (snapshots.length === 0) {
      historyEmptyEl.textContent = entry.is_backing_up
        ? 'Backing up now... the first backup will appear shortly.'
        : 'No backups yet. The first backup runs within the hour.';
      historyEmptyEl.classList.remove('hidden');
      return;
    }

    setShown(historyCard, true);
    // The server already limits the payload to RECENT_LIMIT rows; slice defensively.
    var restoreConfig = window.mindsBackupTable.restoreConfigFor(entry, openRestoreDialog);
    snapshots.slice(0, RECENT_LIMIT).forEach(function (snapshot, index) {
      historyEl.appendChild(
        window.mindsBackupTable.buildSnapshotRow(agentId, snapshot, index === 0, restoreConfig)
      );
    });
    // Rows rendered while an operation runs must come out disabled.
    if (opUi.isRunning()) disableLiveRestoreButtons(true);

    // The total (not the truncated window) drives the "View all N" affordance.
    var total = typeof entry.snapshots_total === 'number' ? entry.snapshots_total : snapshots.length;
    // Use inline display because the anchor's flex utility overrides `hidden`.
    var hasMore = total > RECENT_LIMIT;
    viewAllLink.style.display = hasMore ? '' : 'none';
    if (hasMore) viewAllLabel.textContent = 'View all ' + total + ' backups';
  }

  function renderEntry(entry) {
    // History renders regardless of the verification check_state early-returns
    // below, so drive it up front.
    renderHistory(entry);
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
      statusLine.textContent += ' This workspace is offline; its backups will be checked when it is back online.';
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
    // Only the newest RECENT_LIMIT rows are shown here; the total for the
    // "View all N" link rides along in snapshots_total.
    fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backups?limit=' + RECENT_LIMIT)
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
    opUi.clearError();
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
          opUi.showError('Could not update the verification setting (HTTP ' + resp.status + ').');
          return null;
        }
        // Re-fetching also re-runs the (possibly slow) service check; the
        // spinner keeps showing what we're doing until the fresh state lands.
        return fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backups?limit=' + RECENT_LIMIT)
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
        opUi.showError('Could not update the verification setting (network error).');
      });
  });

  // -- Operation dispatch ----------------------------------------------------

  function startUpdate(isStopChats) {
    opUi.start(
      '/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backup-service/update',
      { stop_chats: isStopChats },
      {
        isCancellable: true,
        label: 'Updating backup software...',
        successMessage: opUi.successMessageFor('backup_update'),
        retryWithStopChats: function () { startUpdate(true); },
      }
    );
  }

  updateBtn.addEventListener('click', function () {
    startUpdate(false);
  });

  // -- Restore confirmation dialog (shared wiring in backup_table.js) --------

  var openRestoreDialog = window.mindsBackupTable.setupRestoreDialog(function (snapshot) {
    opUi.startRestore(snapshot.snapshot_id, false, new Date(snapshot.time).toLocaleString());
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
      opUi.start(
        '/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backup-service/disable', {},
        { label: 'Turning backups off...', successMessage: 'Backups are now turned off for this workspace.' }
      );
      return;
    }
    var body = {
      backup_provider: providerSelect.value,
      api_key_env: apiKeyEnvInput ? apiKeyEnvInput.value : '',
    };
    opUi.start(
      '/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backup-service/configure', body,
      { label: 'Saving backup settings...', successMessage: 'Your backup storage settings were saved.' }
    );
  });

  // -- Init -------------------------------------------------------------------

  opUi.reattach();
  refreshHealth();
})();
