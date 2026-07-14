// Interactivity for the app-level ("Minds") settings sections
// (templates/SettingsSections.jinja), shared by the centered settings modal
// and the full-page browser-mode fallback. Binds by element id, so the
// sections component must appear at most once per page.
(function () {
  // -- Appearance (dark mode) ----------------------------------------------
  //
  // The theme is a persisted per-machine setting rendered server-side on the
  // document root (Base.jinja), so pages painted after the POST land in the
  // new theme automatically. This page is toggled live; in Electron the
  // notifyAppearanceChanged bridge tells the main process to repaint the
  // other views of every window (chrome titlebar + any minds content page).
  var darkToggle = document.getElementById('dark-mode-toggle');
  if (darkToggle) {
    darkToggle.addEventListener('change', function () {
      var enabled = darkToggle.checked;
      document.documentElement.classList.toggle('dark', enabled);
      fetch('/_chrome/appearance', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dark_mode: enabled }),
      })
        .then(function (resp) {
          if (!resp.ok) throw new Error('HTTP ' + resp.status);
          if (window.minds && window.minds.notifyAppearanceChanged) {
            window.minds.notifyAppearanceChanged(enabled);
          }
        })
        .catch(function () {
          // Persisting failed -- revert the optimistic flip so the checkbox
          // reflects what will actually render on the next page load.
          darkToggle.checked = !enabled;
          document.documentElement.classList.toggle('dark', !enabled);
        });
    });
  }

  // -- Error reporting toggles ----------------------------------------------
  var reportToggle = document.getElementById('report-errors-toggle');
  var logsRow = document.getElementById('include-logs-row');
  var logsToggle = document.getElementById('include-logs-toggle');
  if (reportToggle && logsRow && logsToggle) {
    function syncLogsVisibility() {
      if (reportToggle.checked) logsRow.classList.remove('hidden');
      else logsRow.classList.add('hidden');
    }
    syncLogsVisibility();

    function saveErrorReporting(payload) {
      return fetch('/_chrome/error-reporting', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    }

    reportToggle.addEventListener('change', function () {
      var enabled = reportToggle.checked;
      var payload = { report_unexpected_errors: enabled };
      if (!enabled) {
        logsToggle.checked = false;
        payload.include_logs = false;
      }
      syncLogsVisibility();
      saveErrorReporting(payload);
    });

    logsToggle.addEventListener('change', function () {
      saveErrorReporting({ include_logs: logsToggle.checked });
    });
  }

  // -- Default region ---------------------------------------------------------
  var regionSelect = document.getElementById('default-region-select');
  var regionError = document.getElementById('default-region-error');
  if (regionSelect) {
    regionSelect.addEventListener('change', function () {
      if (regionError) regionError.classList.add('hidden');
      fetch('/_chrome/default-region', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ region: regionSelect.value }),
      })
        .then(function (resp) {
          if (resp.ok) return;
          if (regionError) {
            regionError.textContent = 'Could not save the default region (HTTP ' + resp.status + ')';
            regionError.classList.remove('hidden');
          }
        })
        .catch(function () {
          if (regionError) {
            regionError.textContent = 'Could not save the default region (network error)';
            regionError.classList.remove('hidden');
          }
        });
    });
  }

  // -- Backup master password change ------------------------------------
  // Synchronous POST: the server rekeys every existing backed-up
  // workspace's repository, then answers with per-workspace results.
  var newPasswordInput = document.getElementById('backup-new-password');
  var confirmPasswordInput = document.getElementById('backup-new-password-confirm');
  var savePasswordCheckbox = document.getElementById('backup-change-save-password');
  var changeBtn = document.getElementById('backup-change-password-btn');
  var changeSpinner = document.getElementById('backup-change-spinner');
  var changeError = document.getElementById('backup-change-error');
  var changeResults = document.getElementById('backup-change-results');
  if (!changeBtn) return;

  function showChangeError(message) {
    changeError.textContent = message;
    changeError.classList.remove('hidden');
  }

  function appendResultLine(text) {
    var li = document.createElement('li');
    li.textContent = text;
    changeResults.appendChild(li);
  }

  function renderRotationResults(results, isAllOk) {
    changeResults.textContent = '';
    if (!results || results.length === 0) {
      appendResultLine(isAllOk
        ? 'Master password updated. No existing workspaces had backups to rekey.'
        : 'The master password change failed.');
    } else {
      results.forEach(function (entry) {
        appendResultLine(entry.is_ok
          ? (entry.workspace_name + ': rekeyed')
          : (entry.workspace_name + ': FAILED - ' + (entry.error || 'unknown error')));
      });
      appendResultLine(isAllOk
        ? 'Master password updated everywhere.'
        : 'Master password updated; re-run the change to retry the failed workspaces.');
    }
    changeResults.classList.remove('hidden');
  }

  changeBtn.addEventListener('click', function () {
    changeError.classList.add('hidden');
    changeResults.classList.add('hidden');
    if (newPasswordInput.value !== confirmPasswordInput.value) {
      showChangeError('The two passwords do not match.');
      return;
    }
    changeBtn.disabled = true;
    changeSpinner.classList.remove('hidden');
    fetch('/_chrome/backup-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        new_password: newPasswordInput.value,
        new_password_confirm: confirmPasswordInput.value,
        save_password: savePasswordCheckbox.checked,
      }),
    })
      .then(function (resp) {
        return resp.json().then(function (data) { return { status: resp.status, data: data || {} }; });
      })
      .then(function (res) {
        changeBtn.disabled = false;
        changeSpinner.classList.add('hidden');
        if (res.status !== 200) {
          showChangeError(res.data.error || ('The change failed (HTTP ' + res.status + ').'));
          return;
        }
        newPasswordInput.value = '';
        confirmPasswordInput.value = '';
        renderRotationResults(res.data.results, res.data.ok);
      })
      .catch(function () {
        changeBtn.disabled = false;
        changeSpinner.classList.add('hidden');
        showChangeError('The change failed (network error).');
      });
  });
})();
