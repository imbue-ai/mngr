// Backup section of the workspace settings page: shows the combined
// snapshot status + backup-service verification breakdown from this
// workspace's /api/v1/workspaces/<id>/backups, and drives the actions:
//   - the health-checks ("verification") Enable/Disable button (the whole
//     problem/version breakdown only exists while it is on),
//   - "Update backup software" (one idempotent converge; tracked operation
//     polled at /api/v1/workspaces/operations/backup/<id>, with a
//     "Stop chats and try again" follow-up when running chats block it and
//     a Cancel that works while the update is still waiting),
//   - "Change storage location" (enable, change destination, or disable
//     via the "None" provider; same tracked-operation polling).
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

  var historyCard = document.getElementById('backup-history-card');
  var historyEl = document.getElementById('backup-history');
  var historyEmptyEl = document.getElementById('backup-history-empty');
  var viewAllLink = document.getElementById('backup-view-all');
  var viewAllLabel = document.getElementById('backup-view-all-label');

  // The latest known verification state, driving the Enable/Disable label.
  var isVerificationEnabled = true;

  var RECENT_LIMIT = 5;

  // Plain-language problem descriptions; each ends with what to do about it.
  // "Update backup software" (the button right below this list) fixes all of
  // the fixable ones, so they all point there.
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

  // -- Backup history list --------------------------------------------------

  function relativeAgo(iso) {
    var then = Date.parse(iso);
    if (isNaN(then)) return '';
    var s = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (s < 45) return 'just now';
    var m = Math.floor(s / 60);
    if (m < 60) return m + (m === 1 ? ' min ago' : ' mins ago');
    var h = Math.floor(m / 60);
    if (h < 24) return h + (h === 1 ? ' hour ago' : ' hours ago');
    var d = Math.floor(h / 24);
    if (d < 30) return d + (d === 1 ? ' day ago' : ' days ago');
    var mo = Math.floor(d / 30);
    if (mo < 12) return mo + (mo === 1 ? ' month ago' : ' months ago');
    var y = Math.floor(mo / 12);
    return y + (y === 1 ? ' year ago' : ' years ago');
  }

  // Download one snapshot as a zip. Ported from the Landing "download" flow
  // (window.backupExport): the export route restores the snapshot on this
  // machine and streams it back, so this works even for an offline workspace.
  function downloadSnapshot(link, snapshotId) {
    if (link.getAttribute('data-exporting') === '1') return;
    link.setAttribute('data-exporting', '1');
    link.style.pointerEvents = 'none';
    var original = link.textContent;
    link.innerHTML = '<span class="spinner spinner-accent inline-block w-3 h-3 align-middle"></span> Downloading...';
    function restore(text) {
      link.textContent = text;
      link.style.pointerEvents = '';
      link.removeAttribute('data-exporting');
    }
    fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backups/' + encodeURIComponent(snapshotId) + '/export', {
      method: 'POST',
    })
      .then(function (resp) {
        if (!resp.ok) throw new Error('export failed: ' + resp.status);
        var disp = resp.headers.get('Content-Disposition') || '';
        var match = /filename="?([^"]+)"?/.exec(disp);
        var name = match ? match[1] : (agentId + '-backup.zip');
        return resp.blob().then(function (blob) { return { blob: blob, name: name }; });
      })
      .then(function (result) {
        var url = URL.createObjectURL(result.blob);
        var anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = result.name;
        document.body.appendChild(anchor);
        anchor.click();
        document.body.removeChild(anchor);
        URL.revokeObjectURL(url);
        restore(original);
      })
      .catch(function (err) {
        console.error('Backup export failed:', err);
        // A snapshot pruned between load and click 404s here; show it briefly
        // then restore the affordance (a later refresh drops the stale row).
        link.textContent = 'Download failed';
        setTimeout(function () { restore(original); }, 3000);
      });
  }

  // One row of the "Recent backups" table: time (+ "Latest" badge on the
  // newest) on the left, Download / Restore on the right. Rows after the
  // first carry a top divider; the latest row is tinted. (Snapshot size is
  // deliberately not shown: the workspace's restic predates the per-snapshot
  // size summary, so there is no reliable size to display yet.)
  function buildSnapshotRow(snapshot, isLatest, isFirst) {
    var row = document.createElement('div');
    row.className = 'flex items-center gap-4 px-4 py-3'
      + (isFirst ? '' : ' border-t border-default')
      + (isLatest ? ' bg-fill-hover' : '');

    var timeCell = document.createElement('div');
    timeCell.className = 'flex-1 flex items-center gap-2 min-w-0';

    var timeEl = document.createElement('span');
    timeEl.className = 'type-body text-primary';
    timeEl.textContent = relativeAgo(snapshot.time);
    timeEl.title = new Date(snapshot.time).toLocaleString();
    timeCell.appendChild(timeEl);

    if (isLatest) {
      // Same green pill as the landing page's "Backed up ..." badge.
      var latestBadge = document.createElement('span');
      latestBadge.className = 'inline-flex items-center px-2 py-0.5 rounded-md type-label bg-success/15 text-success';
      latestBadge.textContent = 'Latest';
      timeCell.appendChild(latestBadge);
    }
    row.appendChild(timeCell);

    var actions = document.createElement('div');
    actions.className = 'flex items-center gap-4 shrink-0';

    var download = document.createElement('a');
    download.href = '#';
    download.className = 'type-body text-accent cursor-pointer';
    download.textContent = 'Download';
    download.addEventListener('click', function (ev) {
      ev.preventDefault();
      downloadSnapshot(download, snapshot.snapshot_id);
    });
    actions.appendChild(download);

    // In-place restore is not built yet; the button is shown but inert so the
    // design is visible without implying a working action.
    var restore = document.createElement('button');
    restore.type = 'button';
    restore.disabled = true;
    restore.className = 'type-body text-tertiary cursor-not-allowed bg-transparent border-0 p-0';
    restore.title = 'In-place restore is coming soon';
    restore.textContent = 'Restore';
    actions.appendChild(restore);

    row.appendChild(actions);
    return row;
  }

  // Render the "Recent backups" table from the /backups entry. Independent of
  // the verification check_state below -- the snapshot list comes from restic
  // run on this machine, so it renders even when the workspace is offline. Only
  // the newest RECENT_LIMIT snapshots are shown; the "View all backups" footer
  // links to the paginated full-history page and only appears when there are
  // more snapshots than the table shows.
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
    // restic lists snapshots oldest first; the table wants newest at the top.
    var snapshots = (entry.snapshots || []).slice().sort(function (a, b) {
      return Date.parse(b.time) - Date.parse(a.time);
    });
    if (snapshots.length === 0) {
      historyEmptyEl.textContent = entry.is_backing_up
        ? 'Backing up now... the first backup will appear shortly.'
        : 'No backups yet. The first backup runs within the hour.';
      historyEmptyEl.classList.remove('hidden');
      return;
    }

    setShown(historyCard, true);
    snapshots.slice(0, RECENT_LIMIT).forEach(function (snapshot, index) {
      historyEl.appendChild(buildSnapshotRow(snapshot, index === 0, index === 0));
    });

    // The footer is pointless when the table already shows everything, so it
    // only appears when there are more snapshots than rows -- and then says
    // how many. Visibility is driven via style.display because a `hidden`
    // class would lose to the anchor's own `flex` display utility.
    var hasMore = snapshots.length > RECENT_LIMIT;
    viewAllLink.style.display = hasMore ? '' : 'none';
    if (hasMore) viewAllLabel.textContent = 'View all ' + snapshots.length + ' backups';
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
    // One friendly sentence instead of the old installed/minimum/target
    // triple: whether an update is available is the only thing a user can
    // act on (the CODE_OUTDATED problem below covers "too old to work").
    if (entry.installed_version) {
      if (entry.update_target_version && entry.update_target_version !== entry.installed_version) {
        versionsEl.textContent =
          'Backup software version: ' + entry.installed_version +
          ' (an update to ' + entry.update_target_version + ' is available).';
      } else {
        versionsEl.textContent = 'Backup software version: ' + entry.installed_version + '.';
      }
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
      // backend operation is still running -- keep polling (like creating.js).
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
