// Interactivity for the app-level ("Minds") settings sections
// (templates/AppSettingsSections.jinja), shared by the centered settings
// modal and the full-page browser-mode fallback. Binds by element id /
// class, so the sections component must appear at most once per page.
(function () {
  // -- Left-nav subsection switching ---------------------------------------
  var navButtons = document.querySelectorAll('[data-settings-nav]');
  var panels = document.querySelectorAll('[data-settings-panel]');
  if (navButtons.length && panels.length) {
    function selectSection(name) {
      navButtons.forEach(function (btn) {
        var isActive = btn.getAttribute('data-settings-nav') === name;
        btn.classList.toggle('bg-fill-hover', isActive);
        btn.classList.toggle('text-primary', isActive);
        btn.classList.toggle('text-secondary', !isActive);
      });
      panels.forEach(function (panel) {
        panel.classList.toggle('hidden', panel.getAttribute('data-settings-panel') !== name);
      });
      // Remember the active section in the URL hash so a revoke's reload (or a
      // manual refresh of the full page) restores the same tab instead of
      // snapping to the first. Harmless in the modal iframe.
      try { history.replaceState(null, '', '#' + name); } catch (e) {}
    }
    navButtons.forEach(function (btn) {
      btn.addEventListener('click', function () {
        selectSection(btn.getAttribute('data-settings-nav'));
      });
    });
    // Restore the active section from the URL hash on load (survives the
    // reload a revoke triggers). Falls back to the markup default (Connectors).
    var sectionNames = Array.prototype.map.call(navButtons, function (btn) {
      return btn.getAttribute('data-settings-nav');
    });
    var initialSection = (window.location.hash || '').replace(/^#/, '');
    if (sectionNames.indexOf(initialSection) !== -1) {
      selectSection(initialSection);
    }
  }

  // -- Permission revocation -------------------------------------------
  var revokeDialog = document.getElementById('revoke-dialog');
  var revokeTitle = document.getElementById('revoke-dialog-title');
  var revokeBody = document.getElementById('revoke-dialog-body');
  var revokeCancelBtn = document.getElementById('revoke-cancel-btn');
  var revokeConfirmBtn = document.getElementById('revoke-confirm-btn');
  var revokeErrorEl = document.getElementById('revoke-error');
  var pendingRevoke = null;

  function openRevokeDialog(title, body, request) {
    pendingRevoke = request;
    revokeTitle.textContent = title;
    revokeBody.textContent = body;
    if (revokeErrorEl) revokeErrorEl.classList.add('hidden');
    revokeConfirmBtn.disabled = false;
    revokeDialog.classList.remove('hidden');
  }
  function closeRevokeDialog() {
    pendingRevoke = null;
    revokeDialog.classList.add('hidden');
  }
  if (revokeDialog) {
    revokeCancelBtn.addEventListener('click', closeRevokeDialog);
    revokeDialog.addEventListener('click', function (e) {
      if (e.target === revokeDialog) closeRevokeDialog();
    });
    revokeConfirmBtn.addEventListener('click', function () {
      if (!pendingRevoke) return;
      revokeConfirmBtn.disabled = true;
      fetch(pendingRevoke.url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(pendingRevoke.body),
      })
        .then(function (resp) {
          if (resp.ok) {
            window.location.reload();
            return;
          }
          revokeConfirmBtn.disabled = false;
          if (revokeErrorEl) {
            revokeErrorEl.textContent = 'Could not revoke (HTTP ' + resp.status + ')';
            revokeErrorEl.classList.remove('hidden');
          }
        })
        .catch(function () {
          revokeConfirmBtn.disabled = false;
          if (revokeErrorEl) {
            revokeErrorEl.textContent = 'Could not revoke (network error)';
            revokeErrorEl.classList.remove('hidden');
          }
        });
    });
  }

  document.querySelectorAll('.revoke-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var serviceSection = btn.closest('[data-service-name]');
      var card = btn.closest('[data-workspace-agent-id]');
      var serviceLabel = serviceSection.getAttribute('data-service-label');
      var workspaceName = card.getAttribute('data-workspace-name');
      openRevokeDialog(
        'Revoke ' + serviceLabel + ' access?',
        'This removes ' + workspaceName + "'s " + serviceLabel + ' permissions. The agent can request them again later.',
        {
          url: '/settings/permissions/revoke',
          body: {
            workspace_agent_id: card.getAttribute('data-workspace-agent-id'),
            service_name: serviceSection.getAttribute('data-service-name'),
          },
        }
      );
    });
  });

  document.querySelectorAll('.remove-all-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var serviceSection = btn.closest('[data-service-name]');
      var serviceLabel = serviceSection.getAttribute('data-service-label');
      openRevokeDialog(
        'Remove all ' + serviceLabel + ' authorizations?',
        'This removes ' + serviceLabel + ' permissions from every workspace. Agents can request them again later.',
        {
          url: '/settings/permissions/revoke-all',
          body: { service_name: serviceSection.getAttribute('data-service-name') },
        }
      );
    });
  });

  // File-sharing revocation (own section, own endpoints; no service name).
  document.querySelectorAll('.revoke-fs-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var card = btn.closest('[data-workspace-agent-id]');
      var workspaceName = card.getAttribute('data-workspace-name');
      openRevokeDialog(
        'Revoke file sharing?',
        'This removes ' + workspaceName + "'s shared file access. The agent can request it again later.",
        {
          url: '/settings/permissions/file-sharing/revoke',
          body: { workspace_agent_id: card.getAttribute('data-workspace-agent-id') },
        }
      );
    });
  });

  document.querySelectorAll('.remove-all-fs-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      openRevokeDialog(
        'Remove all file sharing?',
        'This removes shared file access from every workspace. Agents can request it again later.',
        { url: '/settings/permissions/file-sharing/revoke-all', body: {} }
      );
    });
  });

  // Cross-workspace management revocation: one verb, for one granting
  // workspace, across every target it covers. The granting workspace is
  // carried by the enclosing group; the verb by the row.
  document.querySelectorAll('.revoke-verb-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var group = btn.closest('[data-workspace-agent-id]');
      var row = btn.closest('[data-verb-permission]');
      var workspaceName = group.getAttribute('data-workspace-name');
      var verbLabel = row.getAttribute('data-verb-label');
      openRevokeDialog(
        'Revoke ' + verbLabel + ' access?',
        'This removes ' + workspaceName + "'s " + verbLabel + ' access to other workspaces. The agent can request it again later.',
        {
          url: '/settings/permissions/workspace/revoke',
          body: {
            workspace_agent_id: group.getAttribute('data-workspace-agent-id'),
            verb: row.getAttribute('data-verb-permission'),
          },
        }
      );
    });
  });

  // -- Error reporting opt-out -------------------------------------------
  // A single per-machine flag gating both automatic error sends and their log
  // attachments. Saved live (the backend reads it per Sentry event), so the
  // change takes effect without a restart; no reload needed.
  var reportToggle = document.getElementById('report-errors-toggle');
  if (reportToggle) {
    reportToggle.addEventListener('change', function () {
      fetch('/_chrome/error-reporting', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ report_unexpected_errors: reportToggle.checked }),
      })
        .then(function (resp) {
          // An HTTP-error response resolves (does not reject), so revert here too:
          // a failed save leaves the persisted flag unchanged, so reflect that by
          // reverting the checkbox to match what is actually stored.
          if (!resp.ok) reportToggle.checked = !reportToggle.checked;
        })
        .catch(function () {
          // Network error: same reasoning -- nothing was persisted, so revert.
          reportToggle.checked = !reportToggle.checked;
        });
    });
  }

  // -- Sync master password change ---------------------------------------
  // Synchronous POST: the server rewraps each signed-in account's sync key
  // (and pushes/clears the synced secrets), then answers with per-account
  // results. Workspace backup repositories are never touched.
  var newPasswordInput = document.getElementById('backup-new-password');
  var confirmPasswordInput = document.getElementById('backup-new-password-confirm');
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

  function renderChangeResults(results, isAllOk) {
    changeResults.textContent = '';
    if (!results || results.length === 0) {
      appendResultLine('The master password change failed.');
    } else {
      results.forEach(function (entry) {
        appendResultLine(entry.is_ok
          ? (entry.account + ': updated')
          : (entry.account + ': FAILED - ' + (entry.error || 'unknown error')));
      });
      appendResultLine(isAllOk
        ? 'Master password updated for every account.'
        : 'Re-run the change to retry the failed accounts.');
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
        renderChangeResults(res.data.results, res.data.ok);
      })
      .catch(function () {
        changeBtn.disabled = false;
        changeSpinner.classList.add('hidden');
        showChangeError('The change failed (network error).');
      });
  });
})();
