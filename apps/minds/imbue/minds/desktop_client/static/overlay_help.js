// Get-help overlay module. Registers the help modal in the overlay host's
// registry (window.MINDS_OVERLAY_MODALS) so overlay.js renders it as in-page
// DOM: it fetches /help?...&fragment=1, injects the panel, then calls this
// module's init(container). This ports the Help page's former inline script
// (which still runs on the browser full page), scoped to the injected
// container. The host owns the backdrop click-outside dismiss and main owns
// Escape, so this module wires neither.
(function () {
  window.MINDS_OVERLAY_MODALS = window.MINDS_OVERLAY_MODALS || {};

  // Sticky checkbox state: persist each box's checked value across reports so a
  // user who files several reports does not re-tick the same diagnostics.
  var STICKY_PREFIX = 'minds.help.';
  var STICKY_IDS = ['help-include-logs', 'help-app-diagnostics', 'help-workspace-details', 'help-remote-access'];

  window.MINDS_OVERLAY_MODALS.help = {
    // Full-window dim backdrop with a centered dialog (fragment paints the
    // backdrop; the host wires click-outside dismiss for this mode).
    positioning: 'backdrop',

    init: function (container) {
      function find(selector) {
        return container.querySelector(selector);
      }

      var dialog = find('#help-dialog');
      var workspaceAgentId = (dialog && dialog.dataset.workspaceAgentId) || '';
      var includeLogsSetting = !!(dialog && dialog.dataset.includeLogs === 'true');

      function restoreSticky(box) {
        var stored = window.localStorage.getItem(STICKY_PREFIX + box.id);
        if (stored !== null) box.checked = stored === 'true';
      }
      function persistSticky(box) {
        try {
          window.localStorage.setItem(STICKY_PREFIX + box.id, box.checked ? 'true' : 'false');
        } catch (e) {
          /* ignore: a full/blocked localStorage just means the value isn't sticky */
        }
      }
      STICKY_IDS.forEach(function (id) {
        var box = find('#' + id);
        if (!box) return; // not rendered (e.g. include-logs hidden, or no workspace)
        try { restoreSticky(box); } catch (e) { /* ignore unreadable localStorage */ }
        box.addEventListener('change', function () { persistSticky(box); });
      });

      // Host-owned dismiss (see requestCloseModal in overlay.js): route close
      // through main so it hides the overlay view and tears down this fragment.
      function closeHelp() {
        if (window.minds && window.minds.closeModal) window.minds.closeModal();
      }
      find('#help-close-btn').addEventListener('click', closeHelp);

      var status = find('#help-status');
      var submit = find('#help-submit');

      // The two modes differ in what submit does and which options show: agent
      // help spawns an /assist chat (the agent gathers its own context, so the
      // report diagnostics are hidden), while report sends a bug report with the
      // chosen diagnostics.
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
      Array.prototype.forEach.call(container.querySelectorAll('input[name="help-mode"]'), function (radio) {
        radio.addEventListener('change', applyMode);
      });
      applyMode();

      // Agent-help swaps the whole modal body to a loading state while the
      // request blocks (the /help/assist route waits for the chat to be created
      // and its first message sent, ~15s).
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
        }).then(function (resp) {
          return resp.json().then(function (data) { return { ok: resp.ok, data: data }; });
        }).then(function (result) {
          if (result.ok) {
            // Creation + first message are done and the new chat tab has already
            // auto-opened in the workspace, so just dismiss the modal.
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
        }).then(function (resp) {
          return resp.json().then(function (data) { return { ok: resp.ok, data: data }; });
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

      // Reveal the confirmation. When Sentry returned an event id, show it with a
      // copy button so the user can quote it; otherwise hide that row (the report
      // was collected but Sentry was inactive, e.g. in dev).
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

      var copyBtn = find('#help-copy-id-btn');
      copyBtn.addEventListener('click', function () {
        var id = find('#help-event-id').textContent;
        try {
          if (navigator.clipboard) navigator.clipboard.writeText(id);
          copyBtn.textContent = 'Copied';
          setTimeout(function () { copyBtn.textContent = 'Copy'; }, 1500);
        } catch (e) {
          /* ignore */
        }
      });

      find('#help-done-btn').addEventListener('click', closeHelp);
    },

    // All listeners are on elements inside the injected container, so removing
    // the container (host teardown) drops them; nothing global to undo.
    destroy: function () {},
  };
})();
