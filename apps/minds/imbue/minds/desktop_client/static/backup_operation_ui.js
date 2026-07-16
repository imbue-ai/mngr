// Shared driver for the tracked backup-operation strip (the
// BackupOperationStrip template component): spinner label, streamed progress
// logs, error/success messages, Cancel, and the "Stop chats and try again"
// retry. Used by the workspace settings page (update / restore / configure)
// and the backup-history page (restore) so the operation UI exists in exactly
// one place -- the backend operation itself is page-agnostic (one tracked
// operation per workspace, polled at /api/v1/workspaces/operations/backup/<id>),
// so whichever page is open can attach to it and show the same live status.
//
// Usage: var opUi = window.mindsBackupOperationUi.setup({
//   agentId: ...,
//   onRunningChange: function (isRunning) {},  // disable page-specific actions
//   onSuccess: function () {},                 // refresh page data after DONE
// });
// then opUi.start(...) / opUi.startRestore(...) / opUi.reattach().
//
// Include after backup_table.js and before the page script.
(function () {
  function setup(options) {
    var agentId = options.agentId;
    var onRunningChange = options.onRunningChange || function () {};
    var onSuccess = options.onSuccess || function () {};

    var operationStrip = document.getElementById('backup-operation-strip');
    var spinner = document.getElementById('backup-op-spinner');
    var progressEl = document.getElementById('backup-op-progress');
    var errorEl = document.getElementById('backup-error');
    var successEl = document.getElementById('backup-success');
    var stopChatsBtn = document.getElementById('backup-stop-chats-btn');
    var stopChatsBtnWrap = document.getElementById('backup-stop-chats-btn-wrap');
    var cancelBtn = document.getElementById('backup-cancel-btn');
    var cancelBtnWrap = document.getElementById('backup-cancel-btn-wrap');

    var isOperationRunning = false;
    // What "Stop chats and try again" retries: set by whichever chat-gated
    // operation (update or restore) was dispatched last.
    var retryWithStopChats = null;
    // The success confirmation for the operation dispatched from this page
    // session; a poller reattached after a reload falls back to a generic
    // per-kind message (it has no dispatch context, e.g. the restored-to time).
    var pendingSuccessMessage = null;
    // The table row button of an in-flight restore, relabeled "Restoring...".
    var restoringRowBtn = null;

    var OPERATION_SUCCESS_MESSAGES = {
      backup_restore: 'The restore completed successfully. A safety backup of your previous state was saved first.',
      backup_update: 'The backup software update completed successfully.',
      backup_configure: 'Your backup settings were updated.',
    };
    var OPERATION_RUNNING_LABELS = {
      backup_restore: 'Restoring a backup...',
      backup_update: 'Updating backup software...',
      backup_configure: 'Changing backup settings...',
    };

    function setShown(el, isShown) {
      if (el) el.classList.toggle('hidden', !isShown);
    }

    // The strip only takes up space while one of its controls is showing.
    function syncOperationStrip() {
      var isAnyVisible = [spinner, progressEl, errorEl, successEl, stopChatsBtnWrap, cancelBtnWrap].some(
        function (el) { return el && !el.classList.contains('hidden'); }
      );
      setShown(operationStrip, isAnyVisible);
    }

    // Error and success are mutually exclusive terminal messages: showing one
    // clears the other, and both persist until the next operation starts.
    function showError(message) {
      successEl.classList.add('hidden');
      errorEl.textContent = message;
      errorEl.classList.remove('hidden');
      syncOperationStrip();
    }
    function clearError() {
      errorEl.classList.add('hidden');
      syncOperationStrip();
    }
    function showSuccess(message) {
      errorEl.classList.add('hidden');
      successEl.textContent = message;
      successEl.classList.remove('hidden');
      syncOperationStrip();
    }
    function clearSuccess() {
      successEl.classList.add('hidden');
      syncOperationStrip();
    }

    // Cancel only affects a still-waiting backup update or restore (the cancel
    // route 404s for configure operations, which have no waiting phase), so the
    // Cancel button is shown only for cancellable operations -- and the poller
    // hides it once the backend reports the operation started mutating
    // (is_cancellable goes false). ``label`` names the operation in the
    // strip's spinner.
    function setOperationRunning(isRunning, isCancellable, label) {
      isOperationRunning = isRunning;
      if (isRunning) spinner.textContent = label || 'Working...';
      spinner.classList.toggle('hidden', !isRunning);
      stopChatsBtn.disabled = isRunning;
      setShown(cancelBtnWrap, isRunning && isCancellable);
      if (!isRunning) {
        progressEl.classList.add('hidden');
        // A failed restore does not re-render the table, so restore the label.
        if (restoringRowBtn) {
          restoringRowBtn.textContent = 'Restore';
          restoringRowBtn = null;
        }
      }
      onRunningChange(isRunning);
      syncOperationStrip();
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
            // The cancel window closes when the operation starts mutating the
            // workspace; hide the button as soon as the backend says so, so it
            // never offers a cancel that would be a no-op.
            setShown(cancelBtnWrap, !!op.is_cancellable);
            syncOperationStrip();
            setTimeout(pollOperation, 2000);
            return;
          }
          setOperationRunning(false);
          setShown(stopChatsBtnWrap, false);
          if (op.is_done) {
            // A destructive multi-minute operation must end with an explicit
            // confirmation, not just a spinner that quietly disappears.
            showSuccess(
              pendingSuccessMessage || OPERATION_SUCCESS_MESSAGES[op.kind] || 'The operation completed successfully.'
            );
            onSuccess();
            return;
          }
          if (op.blocked_chats && op.blocked_chats.length > 0) {
            showError(
              'Chats are running in this workspace (' + op.blocked_chats.join(', ') +
              '). Stop them before continuing; they resume on your next message.'
            );
            // A reattached poller has no retry closure, so it offers no button.
            setShown(stopChatsBtnWrap, !!retryWithStopChats);
            syncOperationStrip();
            return;
          }
          showError(op.error || 'The backup operation failed.');
        })
        // A transient fetch failure must not end the Working state while the
        // backend operation is still running -- keep polling (like creating.js).
        .catch(function () { setTimeout(pollOperation, 2000); });
    }

    // Dispatch one tracked operation and drive the strip until it ends.
    // opts: { isCancellable, label, successMessage, retryWithStopChats }.
    function start(url, body, opts) {
      clearError();
      clearSuccess();
      pendingSuccessMessage = opts.successMessage || null;
      retryWithStopChats = opts.retryWithStopChats || null;
      setOperationRunning(true, !!opts.isCancellable, opts.label);
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

    // The restore dispatch both pages share: strip labels carry the
    // snapshot's local time, and the originating table row (found by its
    // data-snapshot-id, on whichever page) reads "Restoring..." while it runs.
    function startRestore(snapshotId, isStopChats, timeText) {
      start(
        '/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backups/' + encodeURIComponent(snapshotId) + '/restore',
        { stop_chats: isStopChats },
        {
          isCancellable: true,
          label: timeText ? 'Restoring the backup from ' + timeText + '...' : 'Restoring a backup...',
          successMessage: timeText
            ? 'Workspace restored to the backup from ' + timeText + '. A safety backup of your previous state was saved first.'
            : OPERATION_SUCCESS_MESSAGES.backup_restore,
          retryWithStopChats: function () { startRestore(snapshotId, true, timeText); },
        }
      );
      var rowBtn = document.querySelector('.backup-restore-btn[data-snapshot-id="' + snapshotId + '"]');
      if (rowBtn) {
        rowBtn.textContent = 'Restoring...';
        restoringRowBtn = rowBtn;
      }
    }

    // An operation started elsewhere (another page, another window, a
    // previous session) may be running; re-attach so this page resumes its
    // working state, logs, and Cancel instead of showing idle buttons over a
    // busy workspace. Guarded so re-validation while already attached never
    // stacks a second poller/log stream.
    function reattach() {
      if (isOperationRunning) return;
      fetch('/api/v1/workspaces/operations/backup/' + encodeURIComponent(agentId))
        .then(function (resp) { return resp.ok ? resp.json() : null; })
        .then(function (op) {
          if (isOperationRunning) return;
          if (!op || op.status !== 'RUNNING') return;
          setOperationRunning(true, !!op.is_cancellable, OPERATION_RUNNING_LABELS[op.kind] || 'Working...');
          streamOperationLogs();
          pollOperation();
        })
        .catch(function () { /* no running operation to reattach to */ });
    }

    // The server is the single source of truth; each view re-validates
    // exactly when it becomes observable: on load (the page script calls
    // reattach()) and whenever this window becomes visible or focused again
    // -- an operation may have been started from the other page while this
    // one sat idle in the background. No steady-state polling: an idle,
    // unobserved page costs nothing, and a known-running operation is
    // already covered by its own self-terminating poller.
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) reattach();
    });
    window.addEventListener('focus', function () { reattach(); });

    stopChatsBtn.addEventListener('click', function () {
      setShown(stopChatsBtnWrap, false);
      if (retryWithStopChats) retryWithStopChats();
    });
    // The cancel route also cancels a waiting restore (same operation slot).
    // A cancel that arrives after the operation started mutating is refused
    // (409); surface that instead of silently doing nothing.
    cancelBtn.addEventListener('click', function () {
      fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backup-service/update/cancel', { method: 'POST' })
        .then(function (resp) {
          if (resp.ok) return null;
          return resp.json().then(function (data) {
            showError((data && (data.error || data.message)) || 'Could not cancel the operation.');
          });
        })
        .catch(function () {});
    });

    return {
      start: start,
      startRestore: startRestore,
      reattach: reattach,
      isRunning: function () { return isOperationRunning; },
      showError: showError,
      clearError: clearError,
      successMessageFor: function (kind) { return OPERATION_SUCCESS_MESSAGES[kind]; },
    };
  }

  window.mindsBackupOperationUi = { setup: setup };
})();
