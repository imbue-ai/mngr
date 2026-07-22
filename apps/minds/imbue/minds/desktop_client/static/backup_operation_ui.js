// Shared driver for the tracked backup operations. Used by the workspace
// settings page (update / restore / configure) and the backup-history page
// (restore) so the operation UI exists in exactly one place -- the backend
// operation itself is page-agnostic (one tracked operation per workspace,
// polled at /api/v1/workspaces/operations/backup/<id>), so whichever page is
// open can attach to it and show the same live status.
//
// Where an operation reports itself depends on whether it has a row to speak
// from:
//   - a restore acts on one snapshot, so it speaks from that snapshot's table
//     row: "Restoring..." with a Cancel beside it. Identical on both tables.
//   - an update or storage change acts on the whole workspace, so it speaks
//     from the strip (BackupOperationStrip): spinner, progress, Cancel.
// Terminal success/error messages land in the strip's Notices for both.
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
    // Whether the running operation is a restore. A restore reports itself on
    // its own table row -- "Restoring..." with a Cancel beside it -- so the
    // strip shows no spinner, progress or Cancel for one; the row *is* the
    // progress indicator. Keyed on the operation, not on whether the row was
    // found: a strip spinner appearing beside a silent table is exactly the
    // split this removes, so an unidentified restore shows nothing rather
    // than falling back to the strip.
    var isRestoreRunning = false;
    // Which row the running restore belongs to, or null if unknown. Comes from
    // this page's own dispatch, or from the status response when reattaching
    // to a restore started elsewhere.
    var restoringSnapshotId = null;

    var OPERATION_SUCCESS_MESSAGES = {
      backup_restore: 'The restore completed successfully. A safety backup of your previous state was saved first.',
      backup_update: 'The backup software update completed successfully.',
      backup_configure: 'Your backup settings were updated.',
    };
    // Spinner labels for the strip-driven operations only; a restore has no
    // entry because it never reaches the spinner.
    var OPERATION_RUNNING_LABELS = {
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

    // Whether the running operation can still be cancelled, per the backend's
    // latest word. Held here (not passed around) so a table re-render can
    // repaint its rows without knowing anything about the operation.
    var isCancellableNow = false;

    // Paint the in-flight restore onto its row: the Restore action reads
    // "Restoring...", and Cancel sits beside it while the restore is still
    // cancellable. Idempotent and driven entirely off module state, so a page
    // can call it after any render. Rows for other snapshots are reset, which
    // is also what puts "Restore" back after a failed restore that never
    // re-rendered the table.
    function syncRestoreRows() {
      document.querySelectorAll('.backup-restore-btn').forEach(function (btn) {
        var isRestoringThis = restoringSnapshotId !== null && btn.dataset.snapshotId === restoringSnapshotId;
        btn.textContent = isRestoringThis ? 'Restoring...' : 'Restore';
      });
      document.querySelectorAll('.backup-cancel-row-btn').forEach(function (btn) {
        var isRestoringThis = restoringSnapshotId !== null && btn.dataset.snapshotId === restoringSnapshotId;
        btn.classList.toggle('hidden', !(isRestoringThis && isCancellableNow));
      });
    }

    // Cancel only affects a still-waiting backup update or restore (the cancel
    // route 404s for configure operations, which have no waiting phase), so
    // Cancel is offered only for cancellable operations -- and the poller
    // withdraws it once the backend reports the operation started mutating
    // (is_cancellable goes false). A restore shows all of this on its row; the
    // strip's spinner/progress/Cancel are for the workspace-wide operations
    // (update, storage change), which have no row to speak from. ``label``
    // names those in the strip's spinner.
    function setOperationRunning(isRunning, isCancellable, label) {
      isOperationRunning = isRunning;
      isCancellableNow = isRunning && !!isCancellable;
      var isRowDriven = isRestoreRunning;
      if (!isRunning) {
        isRestoreRunning = false;
        restoringSnapshotId = null;
      }
      syncRestoreRows();
      if (isRunning && !isRowDriven) spinner.textContent = label || 'Working...';
      spinner.classList.toggle('hidden', !isRunning || isRowDriven);
      stopChatsBtn.disabled = isRunning;
      setShown(cancelBtnWrap, isCancellableNow && !isRowDriven);
      if (!isRunning) progressEl.classList.add('hidden');
      onRunningChange(isRunning);
      syncOperationStrip();
    }

    // Step-level progress for the strip-driven operations. A restore says all
    // it needs to on its row, so it streams no progress line -- but the stream
    // is still opened, because its terminal frame is what closes the server's
    // log queue.
    function streamOperationLogs() {
      var source = new EventSource('/api/v1/workspaces/operations/backup/' + encodeURIComponent(agentId) + '/logs');
      source.onmessage = function (event) {
        try {
          var frame = JSON.parse(event.data);
          if (frame.log && !isRestoreRunning) {
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
            // workspace; withdraw Cancel as soon as the backend says so, so it
            // never offers a cancel that would be a no-op. Wherever this
            // operation speaks from -- a restore's row, or the strip.
            isCancellableNow = !!op.is_cancellable;
            if (isRestoreRunning) {
              syncRestoreRows();
            } else {
              setShown(cancelBtnWrap, isCancellableNow);
            }
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

    // Dispatch one tracked operation and drive its UI until it ends.
    // opts: { isCancellable, label, successMessage, retryWithStopChats,
    //         isRestore, snapshotId }. isRestore routes the running state onto
    //         snapshotId's row instead of the strip.
    function start(url, body, opts) {
      clearError();
      clearSuccess();
      isRestoreRunning = !!opts.isRestore;
      restoringSnapshotId = opts.snapshotId || null;
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

    // The restore dispatch both pages share. Naming the snapshot before
    // ``start`` is what puts the operation in row-driven mode, so the strip
    // stays out of it: the row reports the restore, and only the terminal
    // success/error message lands in the strip.
    function startRestore(snapshotId, isStopChats, timeText) {
      start(
        '/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backups/' + encodeURIComponent(snapshotId) + '/restore',
        { stop_chats: isStopChats },
        {
          isRestore: true,
          snapshotId: snapshotId,
          isCancellable: true,
          successMessage: timeText
            ? 'Workspace restored to the backup from ' + timeText + '. A safety backup of your previous state was saved first.'
            : OPERATION_SUCCESS_MESSAGES.backup_restore,
          retryWithStopChats: function () { startRestore(snapshotId, true, timeText); },
        }
      );
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
          // A restore names its snapshot, so a page loaded mid-restore marks
          // the same row the dispatching page did -- rather than showing
          // nothing, which would read as an idle workspace and invite a
          // second Restore click (a 409).
          isRestoreRunning = op.kind === 'backup_restore';
          restoringSnapshotId = op.snapshot_id || null;
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

    // One cancel route serves every cancellable backup operation (there is
    // only ever one per workspace), so the strip's Cancel and a row's Cancel
    // do the same thing. A cancel that arrives after the operation started
    // mutating is refused (409); surface that instead of silently doing
    // nothing.
    function requestCancel() {
      fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backup-service/update/cancel', { method: 'POST' })
        .then(function (resp) {
          if (resp.ok) return null;
          return resp.json().then(function (data) {
            showError((data && (data.error || data.message)) || 'Could not cancel the operation.');
          });
        })
        .catch(function () {});
    }
    cancelBtn.addEventListener('click', requestCancel);
    // Delegated: rows are rebuilt on every table render (and on pagination),
    // so binding per row would miss every row built after setup.
    document.addEventListener('click', function (event) {
      var target = event.target;
      if (target && target.classList && target.classList.contains('backup-cancel-row-btn')) {
        requestCancel();
      }
    });

    return {
      start: start,
      startRestore: startRestore,
      reattach: reattach,
      // Repaint the restore row state onto freshly built rows; a page calls
      // this after rendering its table so a render mid-restore (a refresh, or
      // paginating the history page) does not drop "Restoring..." and Cancel.
      syncRows: syncRestoreRows,
      isRunning: function () { return isOperationRunning; },
      showError: showError,
      clearError: clearError,
      successMessageFor: function (kind) { return OPERATION_SUCCESS_MESSAGES[kind]; },
    };
  }

  window.mindsBackupOperationUi = { setup: setup };
})();
