// Workspace settings page: the Resources (CPU/memory) section.
//
// The section starts hidden; it is revealed only when
// GET /api/v1/workspaces/<id>/resources reports the workspace's provider
// supports resizing (docker/lima). Saving is set-only (POST .../resize) and
// never restarts by itself: when the response shows the running host still
// has the old values, a dialog offers "Restart now" (the existing host-scope
// restart operation) or "Apply on next restart" (leave the pending note up).
(function () {
  var root = document.getElementById('workspace-settings');
  if (!root) return;
  var agentId = root.getAttribute('data-agent-id');
  if (!agentId) return;
  var isStale = root.getAttribute('data-is-stale') === 'true';

  var block = document.getElementById('resources-block');
  var section = document.getElementById('resources-section');
  var cpusInput = document.getElementById('resources-cpus-input');
  var memoryInput = document.getElementById('resources-memory-input');
  var cpusCeilingEl = document.getElementById('resources-cpus-ceiling');
  var memoryCeilingEl = document.getElementById('resources-memory-ceiling');
  var warningEl = document.getElementById('resources-warning');
  var errorEl = document.getElementById('resources-error');
  var saveBtn = document.getElementById('resources-save-btn');
  var resetBtn = document.getElementById('resources-reset-btn');
  var savingBadge = document.getElementById('resources-saving-badge');
  var pendingNote = document.getElementById('resources-pending-note');
  var noteRestartBtn = document.getElementById('resources-note-restart-btn');
  var restartDialog = document.getElementById('resources-restart-dialog');
  var restartLaterBtn = document.getElementById('resources-restart-later-btn');
  var restartNowBtn = document.getElementById('resources-restart-now-btn');
  var restartProgressEl = document.getElementById('resources-restart-progress');
  if (!block || !section || !cpusInput || !memoryInput || !saveBtn || !resetBtn) return;

  // Latest capability bounds from the read endpoint (null until loaded).
  var cpuDimension = null;
  var memoryDimension = null;

  function showError(message) {
    errorEl.textContent = message;
    errorEl.classList.remove('hidden');
  }

  function clearError() {
    errorEl.textContent = '';
    errorEl.classList.add('hidden');
  }

  function setSaving(saving) {
    saveBtn.disabled = saving || isStale;
    resetBtn.disabled = saving || isStale;
    if (savingBadge) {
      if (saving) savingBadge.classList.remove('hidden');
      else savingBadge.classList.add('hidden');
    }
  }

  function formatValue(value) {
    return value === null || value === undefined ? '' : String(value);
  }

  function valuesDiffer(configured, actual) {
    // The pending state: the running host's probed values differ from the
    // configured ones. A stopped host (actual === null) is not pending -- the
    // values simply apply on the next start, silently.
    if (!configured || !actual) return false;
    return configured.cpu_count !== actual.cpu_count || configured.memory_gib !== actual.memory_gib;
  }

  function setPendingNoteVisible(visible) {
    if (!pendingNote) return;
    if (visible) pendingNote.classList.remove('hidden');
    else pendingNote.classList.add('hidden');
  }

  function updateCeilingHint(el, dimension) {
    if (!el) return;
    if (dimension && dimension.ceiling !== null && dimension.ceiling !== undefined) {
      el.textContent = 'of ' + dimension.ceiling + ' available';
    } else {
      el.textContent = '';
    }
  }

  function updateOverProvisionWarning() {
    var messages = [];
    var cpus = parseInt(cpusInput.value, 10);
    var memory = parseInt(memoryInput.value, 10);
    if (cpuDimension && cpuDimension.ceiling && !isNaN(cpus) && cpus > cpuDimension.ceiling) {
      messages.push('CPUs above the available ' + cpuDimension.ceiling + ' will be over-provisioned.');
    }
    if (memoryDimension && memoryDimension.ceiling && !isNaN(memory) && memory > memoryDimension.ceiling) {
      messages.push('Memory above the available ' + memoryDimension.ceiling + ' GiB will be over-provisioned.');
    }
    if (messages.length > 0) {
      warningEl.textContent = messages.join(' ');
      warningEl.classList.remove('hidden');
    } else {
      warningEl.textContent = '';
      warningEl.classList.add('hidden');
    }
  }

  function applyResourcesState(data) {
    cpuDimension = data.cpu || null;
    memoryDimension = data.memory_gib || null;
    updateCeilingHint(cpusCeilingEl, cpuDimension);
    updateCeilingHint(memoryCeilingEl, memoryDimension);
    var configured = data.configured || null;
    if (configured) {
      cpusInput.value = formatValue(configured.cpu_count);
      memoryInput.value = formatValue(configured.memory_gib);
    }
    // Docker workspaces that have never been limited report null dimensions:
    // show "no limit" rather than fake numbers.
    cpusInput.placeholder = configured && configured.cpu_count === null ? 'no limit' : '';
    memoryInput.placeholder = configured && configured.memory_gib === null ? 'no limit' : '';
    setPendingNoteVisible(valuesDiffer(configured, data.actual || null));
    updateOverProvisionWarning();
  }

  function loadResources() {
    fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/resources', { credentials: 'same-origin' })
      .then(function (resp) {
        if (!resp.ok) return null;
        return resp.json();
      })
      .then(function (data) {
        if (!data || !data.supported) return;
        applyResourcesState(data);
        block.classList.remove('hidden');
      })
      .catch(function () {
        // Leave the section hidden; resources are a progressive enhancement.
      });
  }

  function collectRequestedValue(input, dimension, label) {
    // Empty input for an unlimited dimension means "leave unlimited" (omit);
    // otherwise the field must hold a positive integer.
    var raw = (input.value || '').trim();
    if (raw === '') return { value: null };
    var parsed = parseInt(raw, 10);
    var minimum = dimension && dimension.minimum ? dimension.minimum : 1;
    if (isNaN(parsed) || String(parsed) !== raw || parsed < minimum) {
      return { error: label + ' must be a whole number of at least ' + minimum + '.' };
    }
    return { value: parsed };
  }

  function postResize(body) {
    setSaving(true);
    clearError();
    fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/resize', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
      .then(function (resp) {
        return resp.json().then(function (data) { return { ok: resp.ok, status: resp.status, body: data }; });
      })
      .then(function (result) {
        setSaving(false);
        if (!result.ok) {
          var message = (result.body && (result.body.error || result.body.message)) ||
            ('Save failed (HTTP ' + result.status + ')');
          showError(message);
          return;
        }
        var configured = result.body.configured || null;
        var actual = result.body.actual || null;
        if (configured) {
          cpusInput.value = formatValue(configured.cpu_count);
          memoryInput.value = formatValue(configured.memory_gib);
          cpusInput.placeholder = configured.cpu_count === null ? 'no limit' : '';
          memoryInput.placeholder = configured.memory_gib === null ? 'no limit' : '';
        }
        updateOverProvisionWarning();
        var isPending = valuesDiffer(configured, actual);
        setPendingNoteVisible(isPending);
        if (isPending && restartDialog) {
          restartDialog.classList.remove('hidden');
        }
      })
      .catch(function (err) {
        setSaving(false);
        showError('Network error saving resources: ' + err.message);
      });
  }

  function pollRestartUntilDone() {
    fetch('/api/v1/workspaces/operations/restart/' + encodeURIComponent(agentId), { credentials: 'same-origin' })
      .then(function (resp) {
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.json();
      })
      .then(function (data) {
        if (!data.is_done) {
          window.setTimeout(pollRestartUntilDone, 2000);
          return;
        }
        if (restartProgressEl) restartProgressEl.classList.add('hidden');
        if (data.error) {
          showError('Restart failed: ' + data.error);
        }
        // Re-read so the pending note clears (or stays, if the restart failed
        // before the new values applied).
        loadResources();
      })
      .catch(function () {
        if (restartProgressEl) restartProgressEl.classList.add('hidden');
        loadResources();
      });
  }

  function startRestart() {
    if (restartDialog) restartDialog.classList.add('hidden');
    if (restartProgressEl) {
      restartProgressEl.textContent = 'Restarting workspace...';
      restartProgressEl.classList.remove('hidden');
    }
    fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/restart', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope: 'host' }),
    })
      .then(function (resp) {
        if (resp.status === 202) {
          window.setTimeout(pollRestartUntilDone, 2000);
          return null;
        }
        return resp.json().then(function (data) {
          if (restartProgressEl) restartProgressEl.classList.add('hidden');
          var message = (data && (data.error || data.message)) || ('Restart failed (HTTP ' + resp.status + ')');
          showError(message);
        });
      })
      .catch(function (err) {
        if (restartProgressEl) restartProgressEl.classList.add('hidden');
        showError('Network error starting restart: ' + err.message);
      });
  }

  saveBtn.addEventListener('click', function () {
    clearError();
    var cpusResult = collectRequestedValue(cpusInput, cpuDimension, 'CPUs');
    if (cpusResult.error) return showError(cpusResult.error);
    var memoryResult = collectRequestedValue(memoryInput, memoryDimension, 'Memory');
    if (memoryResult.error) return showError(memoryResult.error);
    var body = {};
    if (cpusResult.value !== null) body.cpus = cpusResult.value;
    if (memoryResult.value !== null) body.memory_gib = memoryResult.value;
    if (Object.keys(body).length === 0) {
      showError('Enter a CPU or memory value to save.');
      return;
    }
    postResize(body);
  });

  resetBtn.addEventListener('click', function () {
    clearError();
    postResize({ cpus: 'default', memory_gib: 'default' });
  });

  [cpusInput, memoryInput].forEach(function (input) {
    input.addEventListener('input', updateOverProvisionWarning);
  });

  if (restartLaterBtn && restartDialog) {
    restartLaterBtn.addEventListener('click', function () {
      restartDialog.classList.add('hidden');
    });
    restartDialog.addEventListener('click', function (e) {
      if (e.target === restartDialog) restartDialog.classList.add('hidden');
    });
  }
  if (restartNowBtn) restartNowBtn.addEventListener('click', startRestart);
  if (noteRestartBtn) noteRestartBtn.addEventListener('click', startRestart);

  loadResources();
})();
