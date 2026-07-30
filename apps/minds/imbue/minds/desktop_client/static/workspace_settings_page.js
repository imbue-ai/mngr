// Rename + destroy controls for the workspace settings sections.
//
// Extracted from the inline script that used to live in
// pages/WorkspaceSettings.jinja so the same behavior serves both surfaces that
// render WorkspaceSettingsSections: the full settings page and the docked
// options panel (pages.WorkspaceOptionsModal). Everything here is guarded on
// its own elements -- the options panel renders the share pane on some opens
// and this file loads either way.

(function () {
  'use strict';

  var root = document.getElementById('workspace-settings');
  if (!root) return;
  var agentId = root.dataset.agentId;

  // -- Rename ---------------------------------------------------------------
  // Ultra-basic: a text input + Save that POSTs the new (arbitrary) name.
  // The server normalizes it to the host-name slug and updates both the
  // host name and the display label together. Reload on success so every
  // surface (title, sidebar) repaints with the new name.
  var renameInput = document.getElementById('workspace-name-input');
  var renameSaveBtn = document.getElementById('rename-save-btn');
  var renameErrorEl = document.getElementById('rename-error');
  var renameSavingBadge = document.getElementById('rename-saving-badge');
  if (renameInput && renameSaveBtn && renameErrorEl) {
    renameSaveBtn.addEventListener('click', function () {
      var newName = (renameInput.value || '').trim();
      renameErrorEl.classList.add('hidden');
      if (!newName) {
        renameErrorEl.textContent = 'A machine name is required.';
        renameErrorEl.classList.remove('hidden');
        return;
      }
      renameSaveBtn.disabled = true;
      if (renameSavingBadge) renameSavingBadge.classList.remove('hidden');
      fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: newName })
      })
        .then(function (resp) {
          if (resp.ok) {
            window.location.reload();
            return null;
          }
          return resp.json().then(function (data) {
            renameSaveBtn.disabled = false;
            if (renameSavingBadge) renameSavingBadge.classList.add('hidden');
            var message = (data && (data.error || data.message || data.detail)) ||
              ('Rename failed (HTTP ' + resp.status + ')');
            renameErrorEl.textContent = message;
            renameErrorEl.classList.remove('hidden');
          });
        })
        .catch(function () {
          renameSaveBtn.disabled = false;
          if (renameSavingBadge) renameSavingBadge.classList.add('hidden');
          renameErrorEl.textContent = 'Rename failed (network error)';
          renameErrorEl.classList.remove('hidden');
        });
    });
  }

  // -- Destroy --------------------------------------------------------------
  var destroyBtn = document.getElementById('destroy-btn');
  var destroyDialog = document.getElementById('destroy-dialog');
  var destroyCancelBtn = document.getElementById('destroy-cancel-btn');
  var destroyConfirmBtn = document.getElementById('destroy-confirm-btn');
  var destroyErrorEl = document.getElementById('destroy-error');
  if (!destroyBtn || !destroyDialog || !destroyCancelBtn || !destroyConfirmBtn || !destroyErrorEl) return;

  destroyBtn.addEventListener('click', function () {
    destroyDialog.classList.remove('hidden');
  });
  destroyCancelBtn.addEventListener('click', function () {
    destroyDialog.classList.add('hidden');
  });
  destroyDialog.addEventListener('click', function (e) {
    if (e.target === destroyDialog) destroyDialog.classList.add('hidden');
  });

  destroyConfirmBtn.addEventListener('click', function () {
    destroyConfirmBtn.disabled = true;
    // Fire-and-redirect: the detached destroy subprocess survives a
    // settings-page navigation, and the landing page renders a
    // "Destroying..." marker on the row from on-disk state. No more
    // polling here.
    fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/destroy', { method: 'POST' })
      .then(function (resp) {
        if (resp.ok) {
          // In the overlay panel a plain location change would strand the app
          // inside the modal iframe, so hand the navigation to the shell.
          if (window.minds && window.minds.navigateContent) {
            window.minds.navigateContent('/');
            if (window.minds.closeModal) window.minds.closeModal();
          } else {
            window.location.href = '/';
          }
          return;
        }
        destroyConfirmBtn.disabled = false;
        destroyErrorEl.textContent = 'Could not start destroy (HTTP ' + resp.status + ')';
        destroyErrorEl.classList.remove('hidden');
        destroyDialog.classList.add('hidden');
      })
      .catch(function () {
        destroyConfirmBtn.disabled = false;
        destroyErrorEl.textContent = 'Could not start destroy (network error)';
        destroyErrorEl.classList.remove('hidden');
        destroyDialog.classList.add('hidden');
      });
  });
})();
