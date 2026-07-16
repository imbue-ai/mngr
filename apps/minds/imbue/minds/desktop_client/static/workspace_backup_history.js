// Full backup-history page: fills the table one page at a time from
// GET /api/v1/workspaces/<id>/backups?limit=&offset= (newest-first, paginated
// server-side so a long history is never loaded all at once, using the
// snapshots_total count to drive the Newer/Older pagination controls).
//
// The row markup (including the per-snapshot Download flow) and the restore
// confirmation dialog are shared with the settings page's "Recent backups"
// table via window.mindsBackupTable (backup_table.js); the tracked-operation
// strip (progress, Cancel, "Stop chats and try again", success/error) is the
// same BackupOperationStrip + backup_operation_ui.js pair the settings page
// uses. A confirmed restore runs in place on this page, and because the
// backend tracks one operation per workspace, an operation started on either
// page shows live on both (each reattaches on load).
(function () {
  var page = document.getElementById('backup-history-page');
  if (!page) return;
  var agentId = page.dataset.agentId;

  var statusEl = document.getElementById('history-status');
  var cardEl = document.getElementById('history-card');
  var rowsEl = document.getElementById('history-rows');
  var paginationEl = document.getElementById('history-pagination');
  var rangeEl = document.getElementById('history-range');
  var prevBtn = document.getElementById('history-prev-btn');
  var nextBtn = document.getElementById('history-next-btn');

  var PAGE_SIZE = 15;
  var offset = 0;
  var total = 0;

  function setShown(el, isShown) {
    el.classList.toggle('hidden', !isShown);
  }

  function showStatus(message) {
    statusEl.textContent = message;
    setShown(statusEl, true);
    setShown(cardEl, false);
    setShown(paginationEl, false);
  }

  // The shared operation-strip driver: restores dispatched here run in place,
  // and an operation already running (started on any page) is reattached to
  // on load. While one runs, this page's live Restore buttons are disabled;
  // when one succeeds, the table refreshes (the new pre-restore safety
  // snapshot appears).
  var opUi = window.mindsBackupOperationUi.setup({
    agentId: agentId,
    onRunningChange: function (isRunning) {
      page.querySelectorAll('.backup-restore-btn.text-accent').forEach(function (btn) {
        btn.disabled = isRunning;
      });
    },
    onSuccess: function () { loadPage(); },
  });

  var openRestoreDialog = window.mindsBackupTable.setupRestoreDialog(function (snapshot) {
    opUi.startRestore(snapshot.snapshot_id, false, new Date(snapshot.time).toLocaleString());
  });

  function renderPage(pageSnapshots) {
    rowsEl.textContent = '';

    if (total === 0) {
      showStatus('No backups yet. The first backup runs within the hour.');
      return;
    }

    setShown(statusEl, false);
    setShown(cardEl, true);
    pageSnapshots.forEach(function (snapshot, index) {
      // "Latest" marks the repository's newest snapshot, which only appears
      // on the first page.
      var isLatest = offset === 0 && index === 0;
      rowsEl.appendChild(
        window.mindsBackupTable.buildSnapshotRow(agentId, snapshot, isLatest, index === 0, {
          onRestore: openRestoreDialog,
        })
      );
    });
    // Rows rendered while an operation runs (e.g. paginating mid-restore)
    // must come out disabled.
    if (opUi.isRunning()) {
      page.querySelectorAll('.backup-restore-btn.text-accent').forEach(function (btn) {
        btn.disabled = true;
      });
    }

    var first = offset + 1;
    var last = offset + pageSnapshots.length;
    rangeEl.textContent = 'Showing ' + first + '-' + last + ' of ' + total + ' backups';
    prevBtn.disabled = offset === 0;
    nextBtn.disabled = last >= total;
    setShown(paginationEl, total > PAGE_SIZE);
  }

  function loadPage() {
    showStatus('Loading backup history...');
    var url = '/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backups'
      + '?limit=' + PAGE_SIZE + '&offset=' + offset;
    fetch(url)
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (entry) {
        if (!entry) {
          showStatus('Could not load backup history.');
          return;
        }
        if (!entry.is_configured) {
          showStatus('Backups are turned off for this workspace.');
          return;
        }
        if (entry.snapshots_error) {
          showStatus("Couldn't load your backup history right now.");
          return;
        }
        // Fall back to the returned rows if snapshots_total is absent (e.g. an
        // older backend that predates the count) so a present page never
        // collapses to the empty "No backups yet" state.
        var pageSnapshots = entry.snapshots || [];
        total = typeof entry.snapshots_total === 'number' ? entry.snapshots_total : offset + pageSnapshots.length;
        renderPage(pageSnapshots);
      })
      .catch(function () {
        showStatus('Could not load backup history.');
      });
  }

  prevBtn.addEventListener('click', function () {
    offset = Math.max(0, offset - PAGE_SIZE);
    loadPage();
  });
  nextBtn.addEventListener('click', function () {
    offset += PAGE_SIZE;
    loadPage();
  });

  opUi.reattach();
  loadPage();
})();
