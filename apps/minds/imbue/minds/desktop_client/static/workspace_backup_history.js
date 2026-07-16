// Full backup-history page: fills the table one page at a time from
// GET /api/v1/workspaces/<id>/backups/snapshots?offset=&limit= (newest-first,
// paginated server-side so a long history is never loaded all at once) and
// drives the Newer/Older pagination controls.
//
// The row markup (including the per-snapshot Download flow) is shared with
// the settings page's "Recent backups" table via window.mindsBackupTable
// (backup_table.js).
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

  var PAGE_SIZE = 20;
  var offset = 0;

  function setShown(el, isShown) {
    el.classList.toggle('hidden', !isShown);
  }

  function showStatus(message) {
    statusEl.textContent = message;
    setShown(statusEl, true);
    setShown(cardEl, false);
    setShown(paginationEl, false);
  }

  function renderPage(entry) {
    rowsEl.textContent = '';

    if (!entry.is_configured) {
      showStatus('Backups are turned off for this workspace.');
      return;
    }
    if (entry.error) {
      showStatus("Couldn't load your backup history right now.");
      return;
    }
    if (entry.total === 0) {
      showStatus('No backups yet. The first backup runs within the hour.');
      return;
    }

    setShown(statusEl, false);
    setShown(cardEl, true);
    entry.snapshots.forEach(function (snapshot, index) {
      // "Latest" marks the repository's newest snapshot, which only appears
      // on the first page.
      var isLatest = entry.offset === 0 && index === 0;
      rowsEl.appendChild(window.mindsBackupTable.buildSnapshotRow(agentId, snapshot, isLatest, index === 0));
    });

    var first = entry.offset + 1;
    var last = entry.offset + entry.snapshots.length;
    rangeEl.textContent = 'Showing ' + first + '-' + last + ' of ' + entry.total + ' backups';
    prevBtn.disabled = entry.offset === 0;
    nextBtn.disabled = last >= entry.total;
    setShown(paginationEl, entry.total > entry.snapshots.length || entry.offset > 0);
  }

  function loadPage() {
    showStatus('Loading backup history...');
    fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backups/snapshots?offset=' + offset + '&limit=' + PAGE_SIZE)
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (entry) {
        if (!entry) {
          showStatus('Could not load backup history.');
          return;
        }
        // A snapshot pruned between page loads can leave the offset past the
        // end; snap back to the last valid page instead of showing nothing.
        if (entry.total > 0 && entry.snapshots.length === 0 && offset > 0) {
          offset = Math.max(0, Math.floor((entry.total - 1) / PAGE_SIZE) * PAGE_SIZE);
          loadPage();
          return;
        }
        renderPage(entry);
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

  loadPage();
})();
