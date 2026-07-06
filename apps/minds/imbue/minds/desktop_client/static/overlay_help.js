// Get-help modal logic, single-sourced for both contexts (the auth.js pattern):
//   * Electron overlay: registered in window.MINDS_OVERLAY_MODALS so overlay.js
//     injects the ?fragment=1 markup and calls init(container). The overlay host
//     owns the backdrop click-outside dismiss and main owns Escape.
//   * Standalone browser page (/help in the content frame): this file auto-runs
//     init(document) when the help DOM is present, and -- since there is no
//     overlay host / main process -- wires its own backdrop dismiss + Escape and
//     falls back to history.back() for close.
//
// ``initHelp(root)`` scopes every lookup to ``root`` and returns a teardown the
// overlay host calls on close.
(function () {
  window.MINDS_OVERLAY_MODALS = window.MINDS_OVERLAY_MODALS || {};

  var STICKY_PREFIX = 'minds.help.';
  var STICKY_IDS = ['help-include-logs', 'help-app-diagnostics', 'help-workspace-details', 'help-remote-access'];

  function initHelp(root) {
    var isElectron = !!(window.minds && window.minds.closeModal);
    var teardownCallbacks = [];

    function find(selector) {
      return root.querySelector(selector);
    }

    var dialog = find('#help-dialog');
    var workspaceAgentId = (dialog && dialog.dataset.workspaceAgentId) || '';
    var includeLogsSetting = !!(dialog && dialog.dataset.includeLogs === 'true');

    // Electron: route close through main (hides the overlay + tears this down).
    // Standalone: the modal is the whole page, so go back.
    function closeHelp() {
      if (isElectron) window.minds.closeModal();
      else window.history.back();
    }

    function restoreSticky(checkbox) {
      var stored = window.localStorage.getItem(STICKY_PREFIX + checkbox.id);
      if (stored !== null) checkbox.checked = stored === 'true';
    }
    function persistSticky(checkbox) {
      try {
        window.localStorage.setItem(STICKY_PREFIX + checkbox.id, checkbox.checked ? 'true' : 'false');
      } catch (error) {
        /* ignore: a full/blocked localStorage just means the value isn't sticky */
      }
    }
    STICKY_IDS.forEach(function (checkboxId) {
      var checkbox = find('#' + checkboxId);
      if (!checkbox) return; // not rendered (e.g. include-logs hidden, or no workspace)
      try { restoreSticky(checkbox); } catch (error) { /* ignore unreadable localStorage */ }
      checkbox.addEventListener('change', function () { persistSticky(checkbox); });
    });

    find('#help-close-btn').addEventListener('click', closeHelp);

    var status = find('#help-status');
    var submit = find('#help-submit');

    // The two modes differ in what submit does and which options show: agent help
    // spawns an /assist chat (it gathers its own context, so the report
    // diagnostics hide), while report sends a bug report with the diagnostics.
    function selectedMode() {
      var checked = find('input[name="help-mode"]:checked');
      return checked ? checked.value : 'report';
    }
    function applyMode() {
      var isAgent = selectedMode() === 'agent';
      var reportOptions = find('#help-report-options');
      if (reportOptions) reportOptions.classList.toggle('hidden', isAgent);
      submit.textContent = isAgent ? 'Start agent' : 'Send report';
    }
    Array.prototype.forEach.call(root.querySelectorAll('input[name="help-mode"]'), function (radio) {
      radio.addEventListener('change', applyMode);
    });
    applyMode();

    function showAgentLoading() {
      find('#help-form').classList.add('hidden');
      find('#help-loading').classList.remove('hidden');
    }
    function hideAgentLoading() {
      find('#help-loading').classList.add('hidden');
      find('#help-form').classList.remove('hidden');
    }

    function submitAgentHelp(description) {
      submit.disabled = true;
      showAgentLoading();
      fetch('/help/assist', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ description: description, workspace_agent_id: workspaceAgentId }),
      }).then(function (response) {
        return response.json().then(function (data) { return { ok: response.ok, data: data }; });
      }).then(function (result) {
        if (result.ok) {
          closeHelp();
        } else {
          hideAgentLoading();
          submit.disabled = false;
          status.textContent = (result.data && result.data.error) || 'Could not start an agent.';
          status.className = 'type-helper text-danger mt-2';
        }
      }).catch(function () {
        hideAgentLoading();
        submit.disabled = false;
        status.textContent = 'Network error starting the agent.';
        status.className = 'type-helper text-danger mt-2';
      });
    }

    submit.addEventListener('click', function () {
      var description = find('#help-description').value.trim();
      if (!description) {
        status.textContent = 'Please describe the problem first.';
        status.className = 'type-helper text-danger mt-2';
        return;
      }
      if (selectedMode() === 'agent') {
        submitAgentHelp(description);
        return;
      }
      var logsBox = find('#help-include-logs');
      var workspaceBox = find('#help-workspace-details');
      var payload = {
        description: description,
        include_logs: includeLogsSetting || (logsBox ? logsBox.checked : false),
        include_app_diagnostics: find('#help-app-diagnostics').checked,
        include_workspace_details: workspaceBox ? workspaceBox.checked : false,
        remote_access: find('#help-remote-access').checked,
        workspace_agent_id: workspaceAgentId,
      };
      submit.disabled = true;
      status.textContent = 'Sending...';
      status.className = 'type-helper text-tertiary mt-2';
      fetch('/help/report', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }).then(function (response) {
        return response.json().then(function (data) { return { ok: response.ok, data: data }; });
      }).then(function (result) {
        if (result.ok) {
          showSent(result.data && result.data.event_id);
        } else {
          submit.disabled = false;
          status.textContent = (result.data && result.data.error) || 'Could not send the report.';
          status.className = 'type-helper text-danger mt-2';
        }
      }).catch(function () {
        submit.disabled = false;
        status.textContent = 'Network error sending the report.';
        status.className = 'type-helper text-danger mt-2';
      });
    });

    function showSent(eventId) {
      find('#help-form').classList.add('hidden');
      find('#help-sent').classList.remove('hidden');
      var idRow = find('#help-event-id-row');
      var idValue = find('#help-event-id');
      if (eventId) {
        idValue.textContent = eventId;
        idRow.classList.remove('hidden');
      } else {
        idRow.classList.add('hidden');
      }
    }

    var copyButton = find('#help-copy-id-btn');
    copyButton.addEventListener('click', function () {
      var eventId = find('#help-event-id').textContent;
      try {
        if (navigator.clipboard) navigator.clipboard.writeText(eventId);
        copyButton.textContent = 'Copied';
        setTimeout(function () { copyButton.textContent = 'Copy'; }, 1500);
      } catch (error) {
        /* ignore */
      }
    });

    find('#help-done-btn').addEventListener('click', closeHelp);

    // Standalone (browser) affordances: Electron's overlay host owns the backdrop
    // click-outside dismiss and main owns Escape, so wire these only when there is
    // no host (a plain page).
    if (!isElectron) {
      var backdrop = find('#help-backdrop');
      if (backdrop) {
        backdrop.addEventListener('click', function (event) {
          if (event.target === backdrop) closeHelp();
        });
      }
      var onKeydown = function (event) { if (event.key === 'Escape') closeHelp(); };
      document.addEventListener('keydown', onKeydown);
      teardownCallbacks.push(function () { document.removeEventListener('keydown', onKeydown); });
    }

    // The close button's data-tooltip is wired by the overlay host (which calls
    // bindTooltips on each injected fragment) or, on the standalone page, by the
    // global tooltip_triggers.js scan from Base.jinja -- not here.
    return function teardown() {
      teardownCallbacks.forEach(function (callback) { try { callback(); } catch (error) { /* noop */ } });
    };
  }

  // Electron overlay registration.
  var teardown = null;
  window.MINDS_OVERLAY_MODALS.help = {
    positioning: 'backdrop',
    init: function (container) { teardown = initHelp(container); },
    destroy: function () {
      if (teardown) { try { teardown(); } catch (error) { /* noop */ } teardown = null; }
    },
  };

  // Standalone browser page: the help DOM is present at load, so wire it against
  // the document. (In the overlay host there is no help DOM at load, so this is a
  // no-op; the host drives init via the registry above when the modal opens.)
  if (document.getElementById('help-dialog')) initHelp(document);
})();
